import asyncio
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.function_flow_executor import FunctionFlowExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def run_test(input_json):
    executor = FunctionFlowExecutor()
    flow_file = os.path.join(os.path.dirname(__file__), "../services/query_plan/what_attributes.yaml")

    final_state = await executor.execute_flow_async(flow_file, input_json)

    logger.info(f"Final State Keys: {list(final_state.keys())}")

    # Step 1
    entities = final_state.get("found_entity_list", [])
    logger.info(f"Step 1: Found {len(entities)} entities")
    for e in entities:
        logger.info(f"- {e.get('node_id')} ({e.get('node_type')}: {e.get('node_name', 'N/A')})")

    # Step 2
    resolved = final_state.get("resolved_attributes_per_entity", [])
    logger.info(f"Step 2: {len(resolved)} resolution results")
    for i, r in enumerate(resolved):
        logger.info(f"Entity {i}: {len(r) if isinstance(r, list) else r} attributes resolved")

    # Step 2a
    neo4j = final_state.get("neo4j_attributes")
    if neo4j:
        logger.info("Step 2a: Neo4j attributes fetched")
        logger.info(json.dumps(neo4j, indent=2, ensure_ascii=False, default=str))

    # Step 3
    relations = final_state.get("relation_attributes")
    if relations:
        logger.info("Step 3: Relation attributes found")

    # Step 4
    keywords = final_state.get("keyword_results")
    if keywords:
        logger.info("Step 4: Keyword results found")

    print(f"final_state: {final_state}")

    return final_state


async def run_relation_test(input_json):
    executor = FunctionFlowExecutor()
    flow_file = os.path.join(os.path.dirname(__file__), "../services/query_plan/what_relation.yaml")

    final_state = await executor.execute_flow_async(flow_file, input_json)

    logger.info(f"Final State Keys: {list(final_state.keys())}")

    # Step 1: Merged entities
    merged = final_state.get("merged_entities", {})
    logger.info(f"Step 1: Merged entities: {merged}")

    # Step 2: Found entities
    entities = final_state.get("found_entity_list", [])
    logger.info(f"Step 2: Found {len(entities)} entities")
    for e in entities:
        logger.info(
            f"  - {e.get('node_id')} ({e.get('node_type')}: {e.get('node_name', 'N/A')}) score={e.get('score')}"
        )

    # Step 3: Subgraph (Neo4jService output)
    subgraph = final_state.get("subgraph")
    if subgraph:
        direct = subgraph.get("direct_connections", {})
        indirect = subgraph.get("indirect_connections", {})
        direct_count = sum(len(g.get("candidates", [])) for g in direct.values()) if isinstance(direct, dict) else 0
        indirect_count = (
            sum(len(g.get("candidates", [])) for g in indirect.values()) if isinstance(indirect, dict) else 0
        )
        logger.info(f"Step 3: {direct_count} direct, {indirect_count} indirect connections")
    else:
        logger.warning("Step 3: No subgraph found")

    # Step 4: Formatted context
    context = final_state.get("relation_context", "")
    if context:
        logger.info(f"Step 4: Relation context ({len(context)} chars):")
        print("\n" + "=" * 60)
        print(context)
        print("=" * 60 + "\n")

    return final_state


async def run_list_test(input_json):
    executor = FunctionFlowExecutor()
    flow_file = os.path.join(os.path.dirname(__file__), "../services/query_plan/what_list.yaml")

    final_state = await executor.execute_flow_async(flow_file, input_json)

    logger.info(f"Final State Keys: {list(final_state.keys())}")

    # Step 3 Output: Dict{items, total} from list_via_subgraph
    results = final_state.get("list_results", {})
    if results:
        items = results.get("items", []) if isinstance(results, dict) else results
        total = results.get("total", len(items)) if isinstance(results, dict) else len(results)
        logger.info(f"Step 3 Output (list_results): Found {total} total, showing {len(items)} items")
        for item in items[:5]:
            logger.info(f"- {item.get('name')} ({item.get('labels')})")
    else:
        logger.info("Step 3 Output (list_results): Empty")

    # Step 4 Output
    formatted = final_state.get("formatted_answer", "")
    logger.info(f"Step 4 Output (formatted_answer):\n{formatted}")

    return final_state


async def run_count_test(input_json):
    executor = FunctionFlowExecutor()
    flow_file = os.path.join(os.path.dirname(__file__), "../services/query_plan/what_list.yaml")

    final_state = await executor.execute_flow_async(flow_file, input_json)

    logger.info(f"Final State Keys: {list(final_state.keys())}")

    # what_count no longer has a standalone query plan.
    # Count queries now reuse what_list and we read the total from list_results.
    results = final_state.get("list_results", {})
    if results:
        logger.info(
            f"Step 3 Output (list_results): total={results.get('total')}, showing={len(results.get('items', []))}"
        )
    else:
        logger.info("Step 3 Output (list_results): Empty")

    # Step 4 Output
    formatted = final_state.get("formatted_answer", "")
    logger.info(f"Step 4 Output (formatted_answer):\n{formatted}")

    return final_state


async def run_constraint_list_test(input_json):
    executor = FunctionFlowExecutor()
    flow_file = os.path.join(os.path.dirname(__file__), "../services/query_plan/what_constraint_list.yaml")

    final_state = await executor.execute_flow_async(flow_file, input_json)

    logger.info(f"Final State Keys: {list(final_state.keys())}")

    # Step 2: Resolved context entities
    entities = final_state.get("found_entity_list", [])
    logger.info(f"Step 2: Found {len(entities)} context entities")
    for e in entities:
        logger.info(
            f"  - {e.get('node_id')} ({e.get('node_type')}: {e.get('node_name', 'N/A')}) score={e.get('score')}"
        )

    # Step 3: Resolved constraint entities
    constraint_entities = final_state.get("constraint_entity_list", [])
    logger.info(f"Step 3: Found {len(constraint_entities)} constraint entities")
    for e in constraint_entities:
        logger.info(
            f"  - {e.get('node_id')} ({e.get('node_type')}: {e.get('node_name', 'N/A')}) score={e.get('score')}"
        )

    # Step 4a: Relation-based results
    rel_results = final_state.get("relation_results")
    if rel_results:
        logger.info(
            f"Step 4a (relation_results): {rel_results.get('total', 0)} items via {rel_results.get('source', 'N/A')}"
        )
        for item in (rel_results.get("items", []))[:5]:
            logger.info(f"- {item.get('name')} ({item.get('labels')})")
    else:
        logger.info("Step 4a (relation_results): None (skipped or empty)")

    # Step 4b: Attribute-based results
    attr_results = final_state.get("attribute_results")
    if attr_results:
        logger.info(
            f"Step 4b (attribute_results): {attr_results.get('total', 0)} items via {attr_results.get('source', 'N/A')}"
        )
        for item in (attr_results.get("items", []))[:5]:
            logger.info(f"- {item.get('name')} ({item.get('labels')})")
    else:
        logger.info("Step 4b (attribute_results): None (skipped or empty)")

    # Step 5: Merged results
    merged = final_state.get("merged_results", {})
    if merged:
        logger.info(f"Step 5 (merged_results): {merged.get('total', 0)} unique items from {merged.get('sources', [])}")
    else:
        logger.info("Step 5 (merged_results): Empty")

    # Step 6: Formatted answer
    formatted = final_state.get("formatted_answer", "")
    if formatted:
        logger.info("Step 6 (formatted_answer):")
        print("\n" + "=" * 60)
        print(formatted)
        print("=" * 60 + "\n")
    else:
        logger.info("Step 6 (formatted_answer): Empty")

    return final_state


if __name__ == "__main__":
    import sys

    # Default: relation test
    test_type = sys.argv[1] if len(sys.argv) > 1 else "relation"

    if test_type == "attribute":
        input_data = {
            "question_type": "WHAT",
            "intent": "attributes",
            "is_query": True,
            "primary_topic": "major",
            "primary_entities": [{"label": "Major", "text": "Ngành Công nghệ thông tin"}],
            "context_entities": [{"label": "University", "text": "Trường Đại học Gia Định"}],
            "compare_mode": False,
            "compare_targets": [],
            "keywords": ["số tín chỉ"],
            "subtopics": ["curriculum"],
            "ambiguity_level": "low",
            "needs_disambiguation": False,
            "time": None,
            "time_compare": None,
            "potential_entities": [],
            "keyword_attributes": ["total_credits"],
            "needs_intermediate_node": True,
            "original_query": "Số tín chỉ của Ngành Trí tuệ nhân tạo",
        }

        asyncio.run(run_test(input_data))

    elif test_type == "relation":
        # input_data = {
        #     "question_type": "WHAT",
        #     "intent": "relation",
        #     "is_query": True,
        #     "primary_topic": "people",
        #     "primary_entities": [
        #     ],
        #     "context_entities": [
        #     ],
        #     "compare_mode": False,
        #     "compare_targets": [],
        #     "subtopics": [],
        #     "ambiguity_level": "low",
        #     "needs_disambiguation": False,
        #     "time": None,
        #     "time_compare": None,
        #     "potential_entities": [],
        #     "keyword_attribute": ["role"],
        #     "extracted_relations": [
        #         {"source": "Person", "relationship": "WORKS_IN", "target": "Faculty"}
        #     ],
        # }
        # input_data = {
        #     "question_type": "WHAT",
        #     "intent": "relation",
        #     "is_query": True,
        #     "primary_topic": "people",
        #     "primary_entities": [
        #     ],
        #     "context_entities": [
        #     ],
        #     "compare_mode": False,
        #     "compare_targets": [],
        #     "subtopics": [],
        #     "ambiguity_level": "low",
        #     "needs_disambiguation": False,
        #     "time": None,
        #     "time_compare": None,
        #     "potential_entities": [],
        #     "keyword_attribute": ["role"],
        #     "extracted_relations": [
        #         {"source": "Person", "relationship": "WORKS_IN", "target": "Faculty"}
        #     ],
        # }
        input_data = {
            "question_type": "WHAT",
            "intent": "relation",
            "is_query": True,
            "primary_topic": "people",
            "primary_entities": [{"label": "Person", "text": "Phó hiệu trưởng"}],
            "context_entities": [{"label": "University", "text": "Trường Đại học Gia Định"}],
            "compare_mode": False,
            "compare_targets": [],
            "keywords": ["Phó hiệu trưởng"],
            "subtopics": [],
            "ambiguity_level": "low",
            "needs_disambiguation": False,
            "time": None,
            "time_compare": None,
            "potential_entities": [],
            "keyword_attributes": ["role"],
            "needs_intermediate_node": False,
            "original_query": "chủ tịch trường là ai",
        }

        asyncio.run(run_relation_test(input_data))

    elif test_type == "list":
        input_data = {
            "question_type": "WHAT",
            "intent": "constraint-list",
            "is_query": True,
            "primary_topic": "campus",
            "primary_entities": [],
            "context_entities": [{"label": "University", "text": "Trường Gia Định"}],
            "count_enumerate_targets": ["Campus"],
            "compare_mode": False,
            "compare_targets": [],
            "keywords": ["danh sách ngành"],
            "subtopics": [],
            "ambiguity_level": "low",
            "needs_disambiguation": False,
            "time": None,
            "time_compare": None,
            "potential_entities": [],
            "keyword_attributes": [],
            "needs_intermediate_node": True,
            "original_query": "địa chỉ trường",
        }
        asyncio.run(run_list_test(input_data))

    elif test_type == "count":
        # input_data = {
        #     "question_type": "WHAT",
        #     "intent": "count",
        #     "is_query": True,
        #     "primary_topic": "faculty",
        #     "primary_entities": [],
        # }

        input_data = {
            "question_type": "WHAT",
            "intent": "count",
            "is_query": True,
            "primary_topic": "course",
            "primary_entities": [],
            "context_entities": [{"label": "Major", "text": "bigdata"}],
            "count_enumerate_targets": ["Course"],
            "original_query": "ngành bigdata có bao nhiêu môn",
        }
        asyncio.run(run_count_test(input_data))

    elif test_type == "constraint-list":
        # input_data = {
        #     "question_type": "WHAT",
        #     "intent": "constraint-list",
        #     "is_query": True,
        #     "primary_topic": "major",
        #     "primary_entities": [],
        #     "context_entities": [
        #     ],
        #     "keyword_attribute": ["scholarship"],
        # }

        # input_data = {
        #     "question_type": "WHAT",
        #     "intent": "constraint-list",
        #     "is_query": True,
        #     "primary_topic": "major",
        #     "primary_entities": [],
        #     "context_entities": [
        #     ],
        #     "keyword_attribute": ["admission_combination"],
        # }
        input_data = {
            "question_type": "WHAT",
            "intent": "constraint-list",
            "is_query": True,
            "primary_topic": "major",
            "primary_entities": [],
            "context_entities": [{"label": "University", "text": "Trường Đại học Gia Định"}],
            "count_enumerate_targets": ["Major"],
            "compare_mode": False,
            "compare_targets": [],
            "keywords": ["tỷ lệ tốt nghiệp", "90%"],
            "subtopics": [],
            "ambiguity_level": "low",
            "needs_disambiguation": False,
            "time": None,
            "time_compare": None,
            "potential_entities": [],
            "keyword_attributes": ["graduation_rate"],
            "needs_intermediate_node": False,
            "original_query": "ngành nào có tỷ lệ TN > 90%",
        }
        asyncio.run(run_constraint_list_test(input_data))

    else:
        print(f"Unknown test type: {test_type}. Use 'attribute', 'relation', 'list', 'count', or 'constraint-list'")
