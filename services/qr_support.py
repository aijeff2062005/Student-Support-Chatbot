"""QR support-code ingestion helpers.

This module keeps QR image handling outside the agent prompts. It validates image
inputs, decodes QR content, and only exposes a sanitized diagnostic context to the
LLM when the decoded payload matches the support-code JSON contract.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

QR_ERROR_FAST_PATH_STATE_KEY = "qr_error_fast_path"
QR_ERROR_CONTEXT_STATE_KEY = "qr_error_context"
QR_ERROR_ARTIFACT_STATE_KEY = "qr_error_artifact"
QR_ATTACHMENT_NOOP_STATE_KEY = "qr_attachment_noop"

MAX_QR_IMAGE_BYTES = 5 * 1024 * 1024
QR_DOWNLOAD_TIMEOUT_SECONDS = 10
MAX_QR_IMAGE_REDIRECTS = 3
MAX_QR_DECODE_CANDIDATES = 40
MAX_QR_CONTOUR_CROPS = 12
SUPPORTED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg"}
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class QrAttachmentError(ValueError):
    """Raised when an attachment cannot be accepted as a QR image."""


class QrPayloadValidationError(ValueError):
    """Raised when decoded QR text is not a valid support-code payload."""


@dataclass(frozen=True)
class QrImagePayload:
    image_bytes: bytes
    mime_type: str
    filename: str
    source: str


@dataclass(frozen=True)
class QrInspectionResult:
    attachment_seen: bool = False
    image_seen: bool = False
    qr_detected: bool = False
    valid: bool = False
    decoded_text: str | None = None
    context: dict[str, Any] | None = None
    image_payload: QrImagePayload | None = None
    user_message: str | None = None


def build_clear_qr_state_updates() -> dict[str, Any]:
    """Clear QR-only state at the start of every turn unless a valid QR replaces it."""
    return {
        QR_ERROR_FAST_PATH_STATE_KEY: False,
        QR_ERROR_CONTEXT_STATE_KEY: None,
        QR_ERROR_ARTIFACT_STATE_KEY: None,
        QR_ATTACHMENT_NOOP_STATE_KEY: False,
    }


def build_qr_fast_path_state_updates(
    context: dict[str, Any],
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build state updates that route this turn directly to answer_query_agent."""
    context_payload = dict(context)
    if artifact:
        context_payload["artifact"] = artifact

    return {
        **build_clear_qr_state_updates(),
        QR_ERROR_FAST_PATH_STATE_KEY: True,
        QR_ERROR_CONTEXT_STATE_KEY: context_payload,
        QR_ERROR_ARTIFACT_STATE_KEY: artifact,
        "is_query": True,
        "is_admission_topic": True,
        "skip_playbook_scenario": True,
        "query_results": None,
        "multi_query_pairs": None,
        "answer_plan": None,
        "data_crawled": None,
        "confidence_score": None,
        "extra_data": {"attachments": None},
    }


def build_qr_runtime_message(original_message: str | None = None) -> str:
    """Create the text event ADK needs while the real QR data lives in state."""
    clean_message = original_message.strip() if isinstance(original_message, str) else ""
    if clean_message:
        return (
            "Người dùng gửi ảnh QR hỗ trợ hợp lệ. Backend đã đọc và chuẩn hóa lỗi trong "
            f"state `{QR_ERROR_CONTEXT_STATE_KEY}`. Tin nhắn kèm theo của người dùng: {clean_message}"
        )
    return (
        "Người dùng gửi ảnh QR hỗ trợ hợp lệ. Backend đã đọc và chuẩn hóa lỗi trong "
        f"state `{QR_ERROR_CONTEXT_STATE_KEY}`. Hãy hướng dẫn sinh viên dựa trên context này."
    )


def sync_qr_fast_path_from_adk_user_content(callback_context: Any) -> bool:
    """Inspect ADK web inline image uploads and sync QR fast-path state for this turn.

    Custom API/webhook requests already pass QR state into the runner. ADK web upload
    bypasses that layer and sends the local file as `Part.inline_data`, so this bridge
    keeps both entrypoints on the same state contract.
    """
    state = callback_context.state
    state_dict = state.to_dict() if hasattr(state, "to_dict") else dict(state)
    user_content = getattr(callback_context, "user_content", None)
    parts = getattr(user_content, "parts", None)
    text_from_parts = _extract_text_from_parts(parts)

    result = inspect_qr_content_parts(parts)
    if result.attachment_seen:
        if result.valid and result.context:
            state.update(build_qr_fast_path_state_updates(result.context))
            runtime_message = build_qr_runtime_message(text_from_parts)
            state["current_user_message"] = runtime_message
            state["current_user_raw_message"] = runtime_message
            logger.info("ADK web inline upload decoded as valid QR support payload.")
            return True

        state.update(build_clear_qr_state_updates())
        if not text_from_parts:
            state[QR_ATTACHMENT_NOOP_STATE_KEY] = True
        logger.info("ADK web inline upload did not contain a valid QR support payload.")
        return False

    if state_dict.get(QR_ERROR_FAST_PATH_STATE_KEY) or state_dict.get(QR_ATTACHMENT_NOOP_STATE_KEY):
        if _is_backend_qr_runtime_content(user_content):
            return True
        state.update(build_clear_qr_state_updates())

    return False


def inspect_qr_content_parts(parts: Any) -> QrInspectionResult:
    """Return a valid QR context from ADK `Content.parts` inline image data."""
    if not parts:
        return QrInspectionResult()

    first_attachment_error: str | None = None
    first_invalid_qr_result: QrInspectionResult | None = None
    valid_results: list[QrInspectionResult] = []
    saw_attachment = False
    saw_image = False
    saw_decoded_qr = False

    for part in parts:
        inline_data = _get_part_inline_data(part)
        if inline_data is None:
            continue

        saw_attachment = True
        filename = _safe_filename(
            _get_value(part, "filename")
            or _get_value(part, "display_name")
            or _get_value(inline_data, "display_name")
            or "adk-web-upload"
        )
        declared_mime = _normalize_mime_type(
            _get_value(inline_data, "mime_type")
            or _get_value(inline_data, "mimeType")
            or _get_value(part, "mime_type")
            or _get_value(part, "mimeType")
        )

        try:
            image_bytes = _coerce_inline_image_bytes(_get_value(inline_data, "data"))
            mime_type = _validate_image_bytes(image_bytes, declared_mime, filename)
        except QrAttachmentError as exc:
            first_attachment_error = first_attachment_error or str(exc)
            continue

        saw_image = True
        image_payload = QrImagePayload(
            image_bytes=image_bytes,
            mime_type=mime_type,
            filename=filename,
            source="adk_web_inline",
        )
        inspection = _inspect_qr_image_payload(image_payload, invalid_schema_log_prefix="ADK web QR")
        if not inspection.qr_detected:
            continue
        saw_decoded_qr = True
        if inspection.valid:
            valid_results.append(inspection)
            continue
        first_invalid_qr_result = first_invalid_qr_result or inspection

    if valid_results:
        return _combine_valid_qr_inspection_results(valid_results)

    if first_invalid_qr_result:
        return first_invalid_qr_result

    if saw_attachment and saw_image:
        return QrInspectionResult(
            attachment_seen=True,
            image_seen=True,
            qr_detected=saw_decoded_qr,
            user_message="Mình chưa đọc được mã QR hợp lệ. Bạn vui lòng gửi lại ảnh QR rõ hơn nhé.",
        )

    if saw_attachment:
        return QrInspectionResult(
            attachment_seen=True,
            image_seen=False,
            user_message=first_attachment_error
            or "Hiện tại mình chỉ hỗ trợ ảnh PNG/JPG chứa mã QR, chưa xử lý PDF hoặc file khác.",
        )

    return QrInspectionResult()


def parse_qr_error_context(decoded_text: str) -> dict[str, Any]:
    """Parse and sanitize a decoded support QR JSON payload."""
    try:
        payload = json.loads(decoded_text)
    except json.JSONDecodeError as exc:
        raise QrPayloadValidationError("Decoded QR content is not JSON") from exc

    if not isinstance(payload, dict):
        raise QrPayloadValidationError("Decoded QR JSON must be an object")

    response = payload.get("response")
    if not isinstance(response, dict):
        raise QrPayloadValidationError("Decoded QR JSON missing response object")

    response_message = _clean_text(response.get("message"), limit=500)
    errors_raw = response.get("errors")
    if not response_message or not isinstance(errors_raw, list) or not errors_raw:
        raise QrPayloadValidationError("Decoded QR JSON missing response.message or response.errors")

    errors: list[dict[str, str]] = []
    for idx, item in enumerate(errors_raw):
        if not isinstance(item, dict):
            raise QrPayloadValidationError(f"response.errors[{idx}] must be an object")

        code = _clean_text(item.get("code"), limit=64)
        field = _clean_text(item.get("field"), limit=128)
        message = _clean_text(item.get("message"), limit=500)

        if not code or not field or not message:
            raise QrPayloadValidationError(f"response.errors[{idx}] missing code, field, or message")
        if not _CODE_PATTERN.fullmatch(code):
            raise QrPayloadValidationError(f"response.errors[{idx}].code has invalid format")
        if not _FIELD_PATTERN.fullmatch(field):
            raise QrPayloadValidationError(f"response.errors[{idx}].field has invalid format")

        normalized_item = {
            "code": code,
            "field": field,
            "message": message,
        }
        for optional_key in ("title", "description", "detail", "solution", "suggestion", "severity"):
            optional_value = _clean_text(item.get(optional_key), limit=700)
            if optional_value:
                normalized_item[optional_key] = optional_value

        errors.append(normalized_item)

    context: dict[str, Any] = {
        "source": "validated_qr_support_payload",
        "response_message": response_message,
        "errors": errors,
    }
    for optional_key in ("title", "description", "detail", "solution", "suggestion", "severity"):
        optional_value = _clean_text(response.get(optional_key), limit=700)
        if optional_value:
            context[optional_key] = optional_value

    return context


async def inspect_qr_attachments(attachments: Any) -> QrInspectionResult:
    """Return a valid QR context if any attachment is an accepted image with supported QR JSON."""
    attachment_items = list(_iter_attachment_items(attachments))
    if not attachment_items:
        return QrInspectionResult()

    first_attachment_error: str | None = None
    first_invalid_qr_result: QrInspectionResult | None = None
    valid_results: list[QrInspectionResult] = []
    saw_image = False
    saw_decoded_qr = False

    for attachment in attachment_items:
        try:
            image_payload = await _load_image_payload(attachment)
        except QrAttachmentError as exc:
            first_attachment_error = first_attachment_error or str(exc)
            continue

        saw_image = True
        inspection = _inspect_qr_image_payload(image_payload, invalid_schema_log_prefix="Decoded QR")
        if not inspection.qr_detected:
            continue
        saw_decoded_qr = True
        if inspection.valid:
            valid_results.append(inspection)
            continue
        first_invalid_qr_result = first_invalid_qr_result or inspection

    if valid_results:
        return _combine_valid_qr_inspection_results(valid_results)

    if first_invalid_qr_result:
        return first_invalid_qr_result

    if saw_image:
        return QrInspectionResult(
            attachment_seen=True,
            image_seen=True,
            qr_detected=saw_decoded_qr,
            user_message="Mình chưa đọc được mã QR hợp lệ. Bạn vui lòng gửi lại ảnh QR rõ hơn nhé.",
        )

    return QrInspectionResult(
        attachment_seen=True,
        image_seen=False,
        user_message=first_attachment_error
        or "Hiện tại mình chỉ hỗ trợ ảnh PNG/JPG chứa mã QR, chưa xử lý PDF hoặc file khác.",
    )


def _combine_valid_qr_inspection_results(results: list[QrInspectionResult]) -> QrInspectionResult:
    if len(results) == 1:
        return results[0]

    contexts = [result.context for result in results if isinstance(result.context, dict)]
    combined_context = _combine_qr_error_contexts(contexts)
    first_result = results[0]
    return QrInspectionResult(
        attachment_seen=True,
        image_seen=True,
        qr_detected=True,
        valid=True,
        decoded_text=first_result.decoded_text,
        context=combined_context,
        image_payload=first_result.image_payload,
    )


def _combine_qr_error_contexts(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    if len(contexts) == 1:
        return dict(contexts[0])

    response_messages = [
        str(context.get("response_message")).strip()
        for context in contexts
        if str(context.get("response_message") or "").strip()
    ]
    unique_response_messages = list(dict.fromkeys(response_messages))
    combined_errors: list[dict[str, Any]] = []
    support_items: list[dict[str, Any]] = []

    for index, context in enumerate(contexts, start=1):
        item_errors: list[dict[str, Any]] = []
        for error in context.get("errors") or []:
            if not isinstance(error, dict):
                continue
            normalized_error = dict(error)
            normalized_error["source_index"] = index
            combined_errors.append(normalized_error)
            item_errors.append(normalized_error)

        support_items.append(
            {
                "index": index,
                "response_message": context.get("response_message") or "",
                "errors": item_errors,
            }
        )

    return {
        "source": "validated_qr_support_payload",
        "response_message": unique_response_messages[0]
        if len(unique_response_messages) == 1
        else "Nhiều vấn đề cần xử lý",
        "support_item_count": len(contexts),
        "errors": combined_errors,
        "support_items": support_items,
    }


def _iter_attachment_items(attachments: Any):
    if isinstance(attachments, dict):
        nested = attachments.get("attachments")
        if isinstance(nested, list):
            for item in nested:
                coerced = _coerce_attachment_dict(item)
                if coerced:
                    yield coerced
        else:
            yield attachments
    elif isinstance(attachments, list):
        for item in attachments:
            coerced = _coerce_attachment_dict(item)
            if coerced:
                yield coerced


def _coerce_attachment_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return item
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else None
    return None


def _inspect_qr_image_payload(
    image_payload: QrImagePayload,
    *,
    invalid_schema_log_prefix: str,
) -> QrInspectionResult:
    decoded_text = _decode_qr_text(image_payload.image_bytes)
    if not decoded_text:
        return QrInspectionResult(attachment_seen=True, image_seen=True, image_payload=image_payload)

    try:
        context = parse_qr_error_context(decoded_text)
    except QrPayloadValidationError as exc:
        logger.info("%s does not match support-code schema: %s", invalid_schema_log_prefix, exc)
        return QrInspectionResult(
            attachment_seen=True,
            image_seen=True,
            qr_detected=True,
            decoded_text=decoded_text,
            image_payload=image_payload,
            user_message="Mã QR này không thuộc hệ thống hỗ trợ hoặc không đúng định dạng.",
        )

    return QrInspectionResult(
        attachment_seen=True,
        image_seen=True,
        qr_detected=True,
        valid=True,
        decoded_text=decoded_text,
        context=context,
        image_payload=image_payload,
    )


async def _load_image_payload(attachment: dict[str, Any]) -> QrImagePayload:
    declared_mime = _normalize_mime_type(
        attachment.get("mime_type")
        or attachment.get("content_type")
        or attachment.get("mimetype")
        or attachment.get("type")
    )
    filename = _safe_filename(attachment.get("filename") or attachment.get("name") or "qr-upload")

    base64_value = attachment.get("base64") or attachment.get("data") or attachment.get("content")
    if isinstance(base64_value, str) and base64_value.strip():
        image_bytes, mime_from_data_url = _decode_base64_image(base64_value)
        mime_type = _validate_image_bytes(image_bytes, declared_mime or mime_from_data_url, filename)
        return QrImagePayload(image_bytes=image_bytes, mime_type=mime_type, filename=filename, source="base64")

    image_url = attachment.get("image_url") or attachment.get("url") or attachment.get("file_url")
    if isinstance(image_url, str) and image_url.strip():
        image_bytes, mime_from_response = await _download_image(image_url.strip())
        mime_type = _validate_image_bytes(image_bytes, declared_mime or mime_from_response, filename)
        return QrImagePayload(image_bytes=image_bytes, mime_type=mime_type, filename=filename, source="url")

    raise QrAttachmentError("Không tìm thấy dữ liệu ảnh hợp lệ trong attachment.")


def _decode_base64_image(value: str) -> tuple[bytes, str | None]:
    data = value.strip()
    mime_type = None
    if data.startswith("data:"):
        header, separator, encoded = data.partition(",")
        if not separator:
            raise QrAttachmentError("Data URL của ảnh không hợp lệ.")
        mime_type = _normalize_mime_type(header.removeprefix("data:").split(";")[0])
        data = encoded

    try:
        image_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise QrAttachmentError("Dữ liệu ảnh base64 không hợp lệ.") from exc

    if len(image_bytes) > MAX_QR_IMAGE_BYTES:
        raise QrAttachmentError("Ảnh QR vượt quá dung lượng cho phép.")

    return image_bytes, mime_type


def _coerce_inline_image_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        image_bytes = value
    elif isinstance(value, bytearray):
        image_bytes = bytes(value)
    elif isinstance(value, str) and value.strip():
        image_bytes, _ = _decode_base64_image(value)
    else:
        raise QrAttachmentError("Không tìm thấy dữ liệu ảnh hợp lệ trong attachment.")

    if len(image_bytes) > MAX_QR_IMAGE_BYTES:
        raise QrAttachmentError("Ảnh QR vượt quá dung lượng cho phép.")
    return image_bytes


async def _download_image(image_url: str) -> tuple[bytes, str | None]:
    try:
        current_url = image_url
        async with httpx.AsyncClient(timeout=QR_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
            for _ in range(MAX_QR_IMAGE_REDIRECTS + 1):
                parsed = urlparse(current_url)
                if parsed.scheme not in {"http", "https"}:
                    raise QrAttachmentError("Đường dẫn ảnh không hợp lệ.")
                if await _is_blocked_download_host(parsed.hostname):
                    raise QrAttachmentError("Đường dẫn ảnh không hợp lệ.")

                response = await client.get(current_url)
                if response.status_code in REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise QrAttachmentError("Đường dẫn ảnh không hợp lệ.")
                    current_url = urljoin(str(response.url), location)
                    continue

                break
            else:
                raise QrAttachmentError("Đường dẫn ảnh chuyển hướng quá nhiều lần.")

            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_QR_IMAGE_BYTES:
                        raise QrAttachmentError("Ảnh QR vượt quá dung lượng cho phép.")
                except ValueError:
                    pass
            image_bytes = response.content
    except QrAttachmentError:
        raise
    except httpx.HTTPError as exc:
        raise QrAttachmentError("Không tải được ảnh QR từ đường dẫn đã gửi.") from exc

    if len(image_bytes) > MAX_QR_IMAGE_BYTES:
        raise QrAttachmentError("Ảnh QR vượt quá dung lượng cho phép.")

    return image_bytes, _normalize_mime_type(response.headers.get("content-type"))


def _validate_image_bytes(image_bytes: bytes, declared_mime: str | None, filename: str) -> str:
    if not image_bytes:
        raise QrAttachmentError("Ảnh QR rỗng hoặc không tải được.")
    if len(image_bytes) > MAX_QR_IMAGE_BYTES:
        raise QrAttachmentError("Ảnh QR vượt quá dung lượng cho phép.")

    detected_mime = _detect_image_mime(image_bytes)
    if not detected_mime:
        raise QrAttachmentError("File gửi lên không phải ảnh PNG/JPG hợp lệ.")

    if declared_mime and "/" not in declared_mime:
        declared_mime = None
    if declared_mime and declared_mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise QrAttachmentError("Hiện tại mình chỉ hỗ trợ ảnh PNG/JPG chứa mã QR.")

    filename_suffix = _filename_suffix(filename)
    if filename_suffix and filename_suffix not in SUPPORTED_IMAGE_EXTENSIONS:
        raise QrAttachmentError("Hiện tại mình chỉ hỗ trợ ảnh PNG/JPG chứa mã QR.")

    return detected_mime


def _decode_qr_text(image_bytes: bytes) -> str | None:
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        logger.error("OpenCV QR decoder is unavailable: %s", exc)
        return None

    image_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image is None:
        return None

    detector = cv2.QRCodeDetector()
    for candidate in _iter_decode_candidates(cv2, image):
        opencv_text = _decode_qr_text_with_opencv(detector, candidate)
        if opencv_text:
            return opencv_text

        zxing_text = _decode_qr_text_with_zxing(candidate)
        if zxing_text:
            return zxing_text

    return None


def _decode_qr_text_with_opencv(detector: Any, image: Any) -> str | None:
    try:
        data, _, _ = detector.detectAndDecode(image)
    except Exception:
        data = ""
    if isinstance(data, str) and data.strip():
        return data.strip()

    try:
        ok, decoded_info, _, _ = detector.detectAndDecodeMulti(image)
    except Exception:
        ok, decoded_info = False, []
    if ok:
        for item in decoded_info:
            if isinstance(item, str) and item.strip():
                return item.strip()

    return None


def _decode_qr_text_with_zxing(image: Any) -> str | None:
    try:
        import zxingcpp
    except Exception as exc:
        logger.debug("zxing-cpp QR decoder is unavailable: %s", exc)
        return None

    try:
        results = zxingcpp.read_barcodes(image)
    except Exception as exc:
        logger.debug("zxing-cpp QR decode failed: %s", exc)
        return None

    for result in results:
        text = getattr(result, "text", None)
        barcode_format = str(getattr(result, "format", ""))
        if isinstance(text, str) and text.strip() and "QR" in barcode_format.upper():
            return text.strip()

    return None


def _iter_decode_candidates(cv2: Any, image: Any):
    emitted = 0
    for candidate in _iter_full_image_decode_candidates(cv2, image):
        yield candidate
        emitted += 1
        if emitted >= MAX_QR_DECODE_CANDIDATES:
            return

    for crop in _iter_qr_like_crops(cv2, image):
        for candidate in _iter_full_image_decode_candidates(cv2, crop):
            yield candidate
            emitted += 1
            if emitted >= MAX_QR_DECODE_CANDIDATES:
                return


def _iter_full_image_decode_candidates(cv2: Any, image: Any):
    yield image

    gray = _to_grayscale(cv2, image)
    yield gray

    normalized = cv2.equalizeHist(gray)
    yield normalized

    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        yield clahe
    except Exception:
        clahe = normalized

    blurred = cv2.GaussianBlur(gray, (0, 0), 1.0)
    sharpened = cv2.addWeighted(gray, 1.8, blurred, -0.8, 0)
    yield sharpened

    for threshold_source in (gray, clahe, sharpened):
        try:
            _, otsu = cv2.threshold(threshold_source, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            yield otsu
        except Exception:
            pass

    try:
        adaptive = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            2,
        )
        yield adaptive
    except Exception:
        pass

    for scale in _decode_upscale_factors(image):
        yield cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        yield cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def _iter_qr_like_crops(cv2: Any, image: Any):
    gray = _to_grayscale(cv2, image)
    height, width = gray.shape[:2]
    image_area = max(1, height * width)

    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    try:
        _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    except Exception:
        return

    kernel_side = max(5, int(min(height, width) * 0.02))
    if kernel_side % 2 == 0:
        kernel_side += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_side, kernel_side))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 70 or h < 70:
            continue
        aspect_ratio = w / float(h)
        if aspect_ratio < 0.65 or aspect_ratio > 1.55:
            continue
        area = w * h
        area_ratio = area / image_area
        if area_ratio < 0.004 or area_ratio > 0.85:
            continue
        boxes.append((area, x, y, w, h))

    boxes.sort(reverse=True)
    for _, x, y, w, h in boxes[:MAX_QR_CONTOUR_CROPS]:
        margin = max(12, int(max(w, h) * 0.18))
        x1 = max(0, x - margin)
        y1 = max(0, y - margin)
        x2 = min(width, x + w + margin)
        y2 = min(height, y + h + margin)
        crop = image[y1:y2, x1:x2]
        if crop.size:
            yield crop


def _to_grayscale(cv2: Any, image: Any):
    if len(image.shape) == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _decode_upscale_factors(image: Any) -> tuple[float, ...]:
    height, width = image.shape[:2]
    longest_side = max(height, width)
    pixel_count = height * width
    if longest_side < 900:
        return (2.0, 3.0)
    if longest_side < 1800:
        return (1.5, 2.0)
    if pixel_count <= 8_000_000 and longest_side < 5000:
        return (1.25, 2.0)
    return (1.25,)


def _detect_image_mime(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _normalize_mime_type(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.split(";")[0].strip().lower()


def _get_part_inline_data(part: Any) -> Any:
    inline_data = _get_value(part, "inline_data")
    if inline_data is None:
        inline_data = _get_value(part, "inlineData")
    return inline_data


def _get_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _is_backend_qr_runtime_content(content: Any) -> bool:
    parts = getattr(content, "parts", None)
    if not parts:
        return False

    text_chunks = []
    for part in parts:
        text = _get_value(part, "text")
        if isinstance(text, str) and text.strip():
            text_chunks.append(text)
    joined_text = " ".join(text_chunks)
    return "Người dùng gửi ảnh QR hỗ trợ hợp lệ" in joined_text and QR_ERROR_CONTEXT_STATE_KEY in joined_text


def _extract_text_from_parts(parts: Any) -> str | None:
    if not parts:
        return None

    text_chunks = []
    for part in parts:
        text = _get_value(part, "text")
        if isinstance(text, str) and text.strip():
            text_chunks.append(text.strip())

    return "\n".join(text_chunks) if text_chunks else None


def _is_blocked_image_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    normalized = hostname.strip().lower()
    if normalized in {"localhost", "0.0.0.0"} or normalized.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast


async def _is_blocked_download_host(hostname: str | None) -> bool:
    if _is_blocked_image_host(hostname):
        return True

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        logger.info("Cannot resolve QR image host: %s", exc)
        return True

    for info in infos:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            return True

    return False


def _safe_filename(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "qr-upload"
    filename = value.rsplit("/", 1)[-1].strip()
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
    return filename[:120] or "qr-upload"


def _filename_suffix(filename: str) -> str | None:
    if "." not in filename:
        return None
    return "." + filename.rsplit(".", 1)[-1].lower()


def _clean_text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    text = _CONTROL_CHARS_PATTERN.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
