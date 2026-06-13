"""Parallel Web Search System with LLM Integration.

This module provides functionality to run multiple web search methods in parallel
(LLM web search and AI Gate web search API), rerank results using Qwen3-Reranker,
and summarize findings using LLM.

Key Features:
- Parallel execution of LLM web search and API-based web search
- Reranking of search results with configurable threshold
- LLM-based result summarization
- Automatic token management and caching
"""

import asyncio
import json
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

import litellm
import nest_asyncio
import requests

from configs.config_service import get_settings
from configs.llm_client import get_litellm_config, get_rerank_params
from utils.logging_config import get_logger
from utils.web_search_using_llm import llm_web_search

logger = get_logger(__name__)

settings = get_settings()

# Global token cache
_token_cache: dict[str, Any] | None = None
_token_expiry: datetime | None = None

# AI Gate API Configuration

# Reranker Configuration
RERANK_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query. "
    "Prioritize recent information when time-related context is present."
)


def get_ai_gate_token() -> dict[str, Any]:
    """Get access token for AI Gate API with automatic caching.

    Uses in-memory caching to avoid redundant token requests.
    Token is automatically refreshed when it expires.

    Returns:
        Dict containing:
            - access_token: Bearer token for API authentication
            - expires_in: Token validity duration in seconds
            - token_type: Type of token (usually "Bearer")

    Raises:
        requests.RequestException: If token request fails
    """
    global _token_cache, _token_expiry

    # Check if cached token is still valid (with 5-minute buffer)
    if _token_cache and _token_expiry and datetime.now() < _token_expiry - timedelta(minutes=5):
        logger.info("Using cached AI Gate token")
        return _token_cache

    logger.info("Requesting new AI Gate token")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        url = settings.ai_gate_auth_url
        data = {
            "client_id": settings.ai_gate_client_id,
            "grant_type": "client_credentials",
            "client_secret": settings.ai_gate_client_secret,
        }
        response = requests.post(url, headers=headers, data=data, timeout=10)
        # response.text

        token_data = response.json()

        # Cache token and set expiry
        _token_cache = token_data
        _token_expiry = datetime.now() + timedelta(seconds=token_data.get("expires_in", 3600))

        logger.info(f"AI Gate token acquired, expires in {token_data.get('expires_in', 3600)}s")
        return token_data

    except requests.exceptions.RequestException as e:
        traceback.print_exc()
        logger.error(f"Failed to get AI Gate token: {e}")
        raise


async def _single_web_search_request(query: str, include_domains: list[str], access_token: str) -> dict[str, Any]:
    """Execute a single web search request to AI Gate API.

    Args:
        query: Search query string
        include_domains: List of domains to prioritize in search
        access_token: Bearer token for API authentication

    Returns:
        Dict containing search results
    """
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: requests.post(
            settings.ai_gate_search_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            json={"query": query, "include_domains": include_domains},
            timeout=30,
        ),
    )
    response.raise_for_status()
    result = response.json()
    return result.get("data", {})


async def web_search_api(
    query: str, include_domains: list[str] | None = None, is_school: bool = False
) -> dict[str, Any]:
    """Execute web search using AI Gate API with dual parallel requests.

    Runs two parallel requests:
    1. General search with empty include_domains
    2. Targeted search with include_domains = ["https://giadinh.edu.vn/"]

    Results from both searches are merged and deduplicated.

    Args:
        query: Search query string
        include_domains: Optional list of domains (ignored, kept for backward compatibility)
        is_school

    Returns:
        Dict containing:
            - answer: Combined summary answers from both searches
            - results: Merged and deduplicated list of search results
            - follow_up_questions: Combined follow-up questions

    Raises:
        Exception: If both search requests fail
    """
    try:
        # Get authentication token
        token_data = get_ai_gate_token()
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("AI Gate token does not include a valid access_token")

        logger.info(f"Executing dual parallel AI Gate web search for: '{query}'")

        # Run two searches in parallel:
        # 1. General search (empty include_domains)
        # 2. Targeted search (giadinh.edu.vn domain)
        if not is_school:
            results = await asyncio.gather(
                _single_web_search_request(query, [], access_token),
                _single_web_search_request(query, ["https://giadinh.edu.vn/"], access_token),
                return_exceptions=True,
            )
            general_result, targeted_result = results
        else:
            results = await asyncio.gather(
                _single_web_search_request(query, ["https://giadinh.edu.vn/"], access_token), return_exceptions=True
            )
            general_result = {}
            targeted_result = results[0]

        # Initialize merged data
        merged_data = {"answer": "", "results": [], "follow_up_questions": []}

        seen_urls = set()

        # Process general search result
        if isinstance(general_result, dict):
            if general_result.get("answer"):
                merged_data["answer"] = general_result.get("answer", "")

            for result in general_result.get("results") or []:
                url = result.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged_data["results"].append(result)

            # Handle case where follow_up_questions is None (not just missing)
            follow_ups = general_result.get("follow_up_questions") or []
            merged_data["follow_up_questions"].extend(follow_ups)
            logger.info(f"General search returned {len(general_result.get('results') or [])} results")
        else:
            logger.error(f"General search failed: {general_result}")

        # Process targeted search result (giadinh.edu.vn)
        if isinstance(targeted_result, dict):
            # Append targeted answer if general answer is empty
            if not merged_data["answer"] and targeted_result.get("answer"):
                merged_data["answer"] = targeted_result.get("answer", "")
            elif targeted_result.get("answer"):
                # Combine both answers
                merged_data["answer"] += "\n\n" + targeted_result.get("answer", "")

            for result in targeted_result.get("results") or []:
                url = result.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged_data["results"].append(result)

            # Add unique follow-up questions (handle None case)
            existing_questions = set(merged_data["follow_up_questions"])
            for q in targeted_result.get("follow_up_questions") or []:
                if q not in existing_questions:
                    merged_data["follow_up_questions"].append(q)

            logger.info(
                f"Targeted search (giadinh.edu.vn) returned {len(targeted_result.get('results') or [])} results"
            )
        else:
            logger.error(f"Targeted search (giadinh.edu.vn) failed: {targeted_result}")

        # Check if both searches failed
        if isinstance(general_result, Exception) and isinstance(targeted_result, Exception):
            raise Exception(f"Both web searches failed. General: {general_result}, Targeted: {targeted_result}")

        logger.info(f"Dual web search completed: {len(merged_data['results'])} total merged results")

        return merged_data

    except Exception as e:
        traceback.print_exc()
        logger.error(f"Web search API failed: {e}")
        raise


def rerank_web_search_results(
    query: str, web_search_data: dict[str, Any], top_k: int = 4, threshold: float = 0.8
) -> list[dict[str, Any]]:
    """Rerank web search results using Qwen3-Reranker-8B.

    Combines the main answer and individual search results, reranks them
    based on relevance to the query, and returns top-k results above threshold.

    Args:
        query: Original search query
        web_search_data: Data from web_search_api() containing answer and results
        top_k: Maximum number of results to return (default: 4)
        threshold: Minimum rerank score threshold (default: 0.8)

    Returns:
        List of dicts, each containing:
            - content: The text content
            - rerank_score: Relevance score from reranker
            - source: "answer" or "result"
            - url: Source URL (if from results)
            - title: Source title (if from results)
    """

    # Prepare documents for reranking
    documents = []
    metadata = []

    # Add main answer as first document
    answer_text = web_search_data.get("answer", "")
    if answer_text:
        documents.append(answer_text)
        metadata.append({"source": "answer", "content": answer_text})

    # Add all search results
    results = web_search_data.get("results", [])
    for result in results:
        content = result.get("content", "")
        if content:
            documents.append(content)
            metadata.append(
                {
                    "source": "result",
                    "content": content,
                    "url": result.get("url", ""),
                    "title": result.get("title", ""),
                    "original_score": result.get("score", 0.0),
                }
            )

    if not documents:
        logger.warning("No documents to rerank")
        return []

    logger.info(f"Reranking {len(documents)} documents for query: '{query}'")

    try:
        # Call reranker via litellm
        rerank_response = litellm.rerank(
            **get_rerank_params(),
            query=query,
            documents=documents,
            top_n=len(documents),
        )

        scores = [0.0] * len(documents)
        for item in rerank_response.results:
            scores[item["index"]] = float(item["relevance_score"])

        # Combine scores with metadata
        reranked_results = []
        for idx, meta in enumerate(metadata):
            score = scores[idx]
            reranked_results.append(
                {
                    "content": meta["content"],
                    "rerank_score": score,
                    "source": meta["source"],
                    "url": meta.get("url"),
                    "title": meta.get("title"),
                    "original_score": meta.get("original_score"),
                }
            )

        # Sort by rerank score descending
        reranked_results.sort(key=lambda x: x["rerank_score"], reverse=True)

        # Filter by threshold and limit to top_k
        filtered_results = [r for r in reranked_results if r["rerank_score"] >= threshold][:top_k]

        logger.info(
            f"Reranking complete: {len(filtered_results)}/{len(reranked_results)} results above threshold {threshold}"
        )

        if filtered_results:
            logger.info(f"Top score: {filtered_results[0]['rerank_score']:.3f}")

        return filtered_results

    except litellm.exceptions.APIConnectionError as e:
        logger.error(f"Reranker API request failed: {e}")
        fallback = [{"content": meta["content"], "rerank_score": 0.0, **meta} for meta in metadata[:top_k]]
        return fallback
    except Exception as e:
        logger.error(f"Unexpected reranker error: {e}", exc_info=True)
        return []


async def llm_web_search_async(query: str) -> str:
    """Async wrapper for llm_web_search synchronous function.

    Args:
        query: Search query string

    Returns:
        LLM response string
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, llm_web_search, query)


async def parallel_search_and_llm(query: str, is_school: bool = False) -> dict[str, Any]:
    """Execute LLM web search and API web search in parallel.

    Uses asyncio.gather() to run both search methods concurrently,
    then reranks the web search results.

    Args:
        query: Search query string

    Returns:
        Dict containing:
            - llm_web_search: Response from LLM (str or None if failed)
            - web_search_reranked: List of reranked results (or [] if failed)
            - source_types: List of successful source types ("llm_search", "web_search")
            - source_urls: List of URLs from reranked results used for summary
    """
    logger.info(f"Starting parallel search for: '{query}'")

    # Track source types and URLs
    source_types = []
    source_urls = []

    # Run both searches in parallel
    results = await asyncio.gather(
        llm_web_search_async(f"{query} tại trường Đại học Gia Định" if is_school else query),
        web_search_api(query, is_school=is_school),
        return_exceptions=True,  # Don't crash if one fails
    )

    llm_result, web_search_result = results

    # Handle LLM result
    if isinstance(llm_result, Exception):
        logger.error(f"LLM web search failed: {llm_result}")
        llm_result = None
    else:
        logger.info(f"LLM web search completed: {len(llm_result)} chars")
        if llm_result:  # LLM search succeeded with content
            source_types.append("llm_search")

    # Handle web search result and rerank
    reranked_results = []
    if isinstance(web_search_result, Exception):
        logger.error(f"Web search API failed: {web_search_result}")
    else:
        logger.info("Web search API completed, reranking results...")
        reranked_results = rerank_web_search_results(query, web_search_result)

        if reranked_results:  # Web search succeeded with reranked results
            source_types.append("web_search")
            # Extract URLs from reranked results that will be used for summary
            for result in reranked_results:
                url = result.get("url")
                if url and url not in source_urls:
                    source_urls.append(url)

    logger.debug(f"Both searches completed and llm_result: '{llm_result}'")
    logger.debug(f"Both searches completed and web_search_reranked: '{reranked_results}'")
    logger.info(f"Source types: {source_types}, Source URLs: {source_urls}")

    return {
        "llm_web_search": llm_result,
        "web_search_reranked": reranked_results,
        "source_types": source_types,
        "source_urls": source_urls,
    }


async def summarize_search_results(
    combined_results: dict[str, Any], original_query: str, custom_instruction: str | None = None
) -> str:
    """Summarize combined search results using LLM.

    Takes results from both LLM web search and reranked web search,
    and generates a comprehensive summary answer.

    Args:
        combined_results: Dict with llm_web_search and web_search_reranked
        original_query: The original search query
        custom_instruction: Optional custom instruction for summarization

    Returns:
        Summarized answer string
    """
    _s = get_settings()

    # Use custom instruction or default
    if custom_instruction is None:
        custom_instruction = """Bạn là một trợ lý AI thông minh. Nhiệm vụ của bạn là tổng hợp thông tin từ nhiều nguồn và tạo ra câu trả lời hoàn chỉnh, chính xác nhất.

Bạn nhận được 2 nguồn thông tin:
1. llm_web_search: Câu trả lời từ mô hình ngôn ngữ lớn (LLM)
2. web_search_reranked: Các đoạn văn bản đã được xếp hạng theo độ liên quan từ kết quả tìm kiếm web

Hãy:
- Tổng hợp thông tin từ CẢ HAI nguồn
- Ưu tiên thông tin có độ tin cậy cao và mới nhất
- Trả lời NGẮN GỌN, RÕ RÀNG bằng tiếng Việt
- Trích dẫn nguồn nếu cần thiết
- Nếu có mâu thuẫn giữa các nguồn, hãy chỉ ra và giải thích"""

    # Prepare input data
    results_json = json.dumps(combined_results, ensure_ascii=False, indent=2)

    user_prompt = f"""Câu hỏi: {original_query}

Dữ liệu tổng hợp:
{results_json}

Hãy tạo câu trả lời hoàn chỉnh dựa trên dữ liệu trên."""

    logger.info(f"Summarizing search results for: '{original_query}'")

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: litellm.completion(
                **get_litellm_config(_s.litellm_web_search_model, task="web_summary"),
                messages=[
                    {"role": "system", "content": custom_instruction},
                    {"role": "user", "content": user_prompt},
                ],
                reasoning_effort="none",
            ),
        )

        summary = response.choices[0].message.content
        logger.info(f"Summary completed: {len(summary)} chars")

        return summary

    except Exception as e:
        logger.error(f"Summary generation failed: {e}", exc_info=True)
        # Fallback: return LLM result if available
        if combined_results.get("llm_web_search"):
            logger.info("Falling back to LLM web search result")
            return combined_results["llm_web_search"]
        return "Xin lỗi, không thể tạo câu trả lời tổng hợp. Vui lòng thử lại."


async def run_parallel_web_search_with_summary(
    query: str, custom_summary_instruction: str | None = None, is_school: bool = False
) -> dict[str, Any]:
    """Main orchestrator function for parallel web search with summarization.

    This is the primary entry point for the parallel web search system.
    It executes both search methods in parallel, reranks results, and
    generates a comprehensive summary.

    Args:
        query: Search query string
        custom_summary_instruction: Optional custom instruction for LLM summarization

    Returns:
        Dict containing:
            - summary: Final summarized answer string
            - source_types: List of successful source types ("llm_search", "web_search")
            - source_urls: List of URLs from reranked results used for summary

    Example:
    """
    start_time = time.time()

    logger.info(f"{'=' * 80}")
    logger.info(f"PARALLEL WEB SEARCH START: '{query}'")
    logger.info(f"{'=' * 80}")

    try:
        # Step 1: Run parallel searches
        logger.info("Step 1/2: Running parallel searches...")
        combined_results = await parallel_search_and_llm(query, is_school=is_school)

        # Extract metadata from parallel search
        source_types = combined_results.get("source_types", [])
        source_urls = combined_results.get("source_urls", [])

        # Step 2: Summarize results
        logger.info("Step 2/2: Generating summary...")
        final_answer = await summarize_search_results(combined_results, query, custom_summary_instruction)

        elapsed_time = time.time() - start_time

        logger.info(f"{'=' * 80}")
        logger.info(f"PARALLEL WEB SEARCH COMPLETE in {elapsed_time:.2f}s")
        logger.info(f"{'=' * 80}")

        logger.debug(f"run_parallel_web_search_with_summary final_answer: '{final_answer}'")
        logger.info(f"Source types: {source_types}, Source URLs: {source_urls}")

        return {"summary": final_answer, "source_types": source_types, "source_urls": source_urls}

    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Parallel web search failed after {elapsed_time:.2f}s: {e}", exc_info=True)
        return {
            "summary": "Xin lỗi, đã xảy ra lỗi khi xử lý câu hỏi. Vui lòng thử lại sau.",
            "source_types": [],
            "source_urls": [],
        }


# Convenience function for synchronous context
def run_parallel_web_search_sync(
    query: str, custom_summary_instruction: str | None = None, is_school: bool = False
) -> dict[str, Any]:
    coroutine = run_parallel_web_search_with_summary(query, custom_summary_instruction, is_school=is_school)
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    logger.warning("Event loop already running, using nest_asyncio workaround")
    nest_asyncio.apply()
    return running_loop.run_until_complete(coroutine)
