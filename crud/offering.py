"""CRUD operations for Offering/Major recommendations."""

import logging

import httpx

from configs.config_service import get_settings
from schemas.offering import OfferingRequest, OfferingResponse, ScoreRecord

logger = logging.getLogger(__name__)

settings = get_settings()

# Offering API Configuration
OFFERING_API_URL = settings.offering_api_url
OFFERING_API_TIMEOUT = settings.offering_api_timeout_seconds


def get_top_majors(
    expected_industries: list[str],
    major_codes: list[str] | None = None,
    score: dict | ScoreRecord | None = None,
    top_k: int = 3,
) -> OfferingResponse | None:
    try:
        # Build request payload via model validation to handle dynamic runtime inputs safely
        payload_input = {
            "major_codes": major_codes or None,
            "expected_industries": expected_industries,
            "score": score.model_dump() if isinstance(score, ScoreRecord) else score,
            "top_k": top_k,
        }
        request_data = OfferingRequest.model_validate(payload_input)

        payload = request_data.model_dump(exclude_none=True)

        logger.info("" + "=" * 60)
        logger.info(" CALLING TOP-MAJORS API")
        logger.info(f"URL: {OFFERING_API_URL}")
        logger.info(f"Major Codes: {major_codes or []}")
        logger.info(f"Expected Industries: {expected_industries}")
        logger.info(f"Top K: {top_k}")
        logger.info("" + "=" * 60)

        # Call Offering API
        logger.info(" Sending request...")
        with httpx.Client(timeout=OFFERING_API_TIMEOUT) as client:
            response = client.post(
                OFFERING_API_URL,
                json=payload,
                headers={"accept": "application/json", "Content-Type": "application/json"},
            )
            logger.info(f"Response status: {response.status_code}")

            # Log response body for debugging
            if response.status_code != 200:
                logger.info(f"Response body: {response.text}")

            response.raise_for_status()

        # Parse response
        data = response.json()
        logger.info(f"Response data: {data}")

        # Handle empty response
        if not data:
            logger.warning(" Top-majors API returned empty response")
            return OfferingResponse(clubs=[], majors=[])

        # NEW FORMAT: Response is object with "clubs" and "majors" keys
        if isinstance(data, dict):
            clubs_list = data.get("clubs", [])
            majors_list = data.get("majors", [])

            if not majors_list:
                logger.warning(" Top-majors API returned empty majors list")
                return OfferingResponse(clubs=clubs_list, majors=[])

            # Parse into OfferingResponse
            offering_response = OfferingResponse(clubs=clubs_list, majors=majors_list)

        # OLD FORMAT: Backward compatible with array response
        else:
            # API returns nested array: [[major1, major2, ...]]
            # Flatten it to get the actual list of majors
            majors_list = data[0] if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list) else data

            if not majors_list or (isinstance(majors_list, list) and len(majors_list) == 0):
                logger.warning(" Top-majors API returned empty nested list")
                return OfferingResponse(clubs=[], majors=[])

            # Parse into OfferingResponse (no clubs in old format)
            offering_response = OfferingResponse(clubs=[], majors=majors_list)

        # Log results
        logger.info(f" Found {len(offering_response.clubs)} clubs and {len(offering_response.majors)} majors")

        if offering_response.clubs:
            logger.info("  Recommended Clubs:")
            for idx, club in enumerate(offering_response.clubs[:5], 1):
                logger.info(f"{idx}. {club}")

        if offering_response.majors:
            logger.info("  Recommended Majors:")
            for idx, major in enumerate(offering_response.majors[:5], 1):
                salary_min = major.trend_info.salary_range[0] if major.trend_info.salary_range else 0
                salary_max = major.trend_info.salary_range[1] if len(major.trend_info.salary_range) > 1 else 0
                admission_rate = major.successful_admission_rates * 100 if major.successful_admission_rates else 0
                logger.info(
                    f"  {idx}. {major.major_name} (ID: {major.major_id}, Admission: {admission_rate:.0f}%, Salary: {salary_min:,} - {salary_max:,} VND)"
                )

        logger.info("" + "=" * 60)

        return offering_response

    except httpx.TimeoutException:
        logger.warning(f" Offering API timeout after {OFFERING_API_TIMEOUT}s")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(f" Offering API HTTP error: {e}")
        return None
    except (KeyError, ValueError) as e:
        logger.error(f" Failed to parse Offering API response: {e}")
        return None
    except Exception as e:
        logger.error(f" Unexpected error calling Offering API: {e}")
        import traceback

        traceback.print_exc()
        return None
