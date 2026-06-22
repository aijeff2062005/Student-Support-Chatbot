#!/usr/bin/env python3
"""
Test script for WHAT definition and attributes query plans.
Run this to verify the new WHAT query/answer plans work correctly.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dbs.keyword_search_milvus_helper import util
from dbs.milvus_helper import connect_milvus, get_nodes_by_type, search_entity_by_name, semantic_resolve_attributes

# Test cases for WHAT questions
TEST_CASES = [
    # WHAT-DEFINITION tests
    {
        "question": "Ngành CNTT là gì?",
        "intent": "definition",
        "primary_entities": [{"label": "Major", "text": "Ngành CNTT"}],
        "keyword_attribute": ["description"],
        "expected_collection": "knowledge_university_entity",
    },
    {
        "question": "Khoa Công nghệ thông tin là gì?",
        "intent": "definition",
        "primary_entities": [{"label": "Faculty", "text": "Khoa Công nghệ thông tin"}],
        "keyword_attribute": ["description"],
        "expected_collection": "knowledge_university_entity",
    },
    # WHAT-ATTRIBUTES tests
    {
        "question": "Điểm chuẩn ngành CNTT?",
        "intent": "attributes",
        "primary_entities": [{"label": "Major", "text": "Ngành CNTT"}],
        "keyword_attribute": ["cutoff_score"],
        "subtopics": ["cutoff"],
        "expected_collection": "knowledge_university_chunk",
    },
    {
        "question": "Học phí ngành Marketing?",
        "intent": "attributes",
        "primary_entities": [{"label": "Major", "text": "Ngành Marketing"}],
        "keyword_attribute": ["fee_information"],
        "subtopics": ["cost_fee"],
        "expected_collection": "knowledge_university_chunk",
    },
    {
        "question": "Chỉ tiêu tuyển sinh ngành Kế toán?",
        "intent": "attributes",
        "primary_entities": [{"label": "Major", "text": "Ngành Kế toán"}],
        "keyword_attribute": ["quota"],
        "subtopics": ["quota"],
        "expected_collection": "knowledge_university_chunk",
    },
]


def test_entity_search():
    """Test entity resolution via Milvus."""
    print("\n" + "=" * 60)
    print("TEST 1: Entity Resolution (knowledge_university_entity)")
    print("=" * 60)

    for tc in TEST_CASES:
        if tc["intent"] == "definition":
            entity = tc["primary_entities"][0]
            print(f"\n Searching: '{entity['text']}' (type: {entity['label']})")

            results = search_entity_by_name(
                entity_name=[entity["text"]], entity_type=[entity["label"]], threshold=0.5, top_k=3
            )

            if results:
                for r in results[:2]:
                    print(f"    Found: {r['node_name']} (score: {r['score']:.3f})")
                    print(f"      Attributes: {r.get('attribute_keys', [])[:5]}...")
            else:
                print("    No results found")


async def test_attribute_resolution():
    """Test semantic attribute matching."""
    print("\n" + "=" * 60)
    print("TEST 2: Attribute Resolution (knowledge_university_entity_property)")
    print("=" * 60)

    # First, get available attributes for a Major
    nodes = get_nodes_by_type(node_types=["Major"], limit=1)
    if not nodes:
        print("    Could not get Major nodes")
        return

    available_attrs = nodes[0].get("attribute_keys", [])
    print(f"\n Available Major attributes: {available_attrs[:10]}...")

    # Test semantic matching for Vietnamese keywords
    test_keywords = [
        ["học phí", "fee_information"],
        ["điểm chuẩn", "cutoff_score"],
        ["chỉ tiêu", "quota"],
        ["tỷ lệ tốt nghiệp", "graduation_rate"],
    ]

    for vn_keyword, en_expected in test_keywords:
        print(f"\n Resolving: '{vn_keyword}'")
        results = await semantic_resolve_attributes(
            requested_attributes=[vn_keyword],
            available_attribute_keys=available_attrs,
            similarity_threshold=0.5,
            top_k=3,
        )

        if results:
            for r in results[:2]:
                match = "" if en_expected in r["attribute_key"] else "️"
                print(f"   {match} Matched: {r['attribute_key']} (score: {r['score']:.3f})")
        else:
            print("    No matches found")


def test_chunk_search():
    """Test chunk search via hybrid util."""
    print("\n" + "=" * 60)
    print("TEST 3: Chunk Search (knowledge_university_chunk)")
    print("=" * 60)

    test_queries = [
        "Học phí ngành CNTT",
        "Điểm chuẩn năm 2024",
        "Chỉ tiêu tuyển sinh Marketing",
    ]

    for query in test_queries:
        print(f"\n Query: '{query}'")
        result = util.get_best(question=query)

        if result and result.record:
            for rec in result.record[:2]:
                print(f"    Found: {rec.content[:100]}...")
                print(f"      Meta: node_id={rec.metadata.get('node_id')}, attr={rec.metadata.get('attribute')}")
        else:
            print("    No chunks found")


def test_query_plan_loading():
    """Test that query plan YAML files load correctly."""
    print("\n" + "=" * 60)
    print("TEST 4: Query Plan YAML Loading")
    print("=" * 60)

    import yaml

    plan_dir = Path(__file__).parent.parent / "services" / "query_plan"

    for plan_name in ["what_definition", "what_attributes"]:
        plan_path = plan_dir / f"{plan_name}.yaml"
        print(f"\n Loading: {plan_name}.yaml")

        if plan_path.exists():
            with open(plan_path) as f:
                plan = yaml.safe_load(f)

            print(f"    flow_name: {plan.get('flow_name')}")
            print(f"    supported_entity_types: {plan.get('supported_entity_types', [])[:5]}")
            print(f"    traversal_steps: {len(plan.get('traversal_steps', []))} steps")
        else:
            print(f"    File not found: {plan_path}")


def test_answer_plan_loading():
    """Test that answer plan MD files load correctly."""
    print("\n" + "=" * 60)
    print("TEST 5: Answer Plan MD Loading")
    print("=" * 60)

    plan_dir = Path(__file__).parent.parent / "services" / "answer_plan"

    for plan_name in ["what_definition", "what_attributes"]:
        plan_path = plan_dir / f"{plan_name}.md"
        print(f"\n Loading: {plan_name}.md")

        if plan_path.exists():
            content = plan_path.read_text(encoding="utf-8")
            lines = content.split("\n")

            # Check for key sections
            has_objective = any("OBJECTIVE" in line for line in lines)
            has_evidence = any("EVIDENCE" in line for line in lines)
            has_structure = any("RESPONSE STRUCTURE" in line or "STRUCTURE" in line for line in lines)

            print(f"    Has OBJECTIVE section: {has_objective}")
            print(f"    Has EVIDENCE section: {has_evidence}")
            print(f"    Has RESPONSE STRUCTURE section: {has_structure}")
            print(f"    Total lines: {len(lines)}")
        else:
            print(f"    File not found: {plan_path}")


async def main():
    """Run all tests."""
    print("\n" + " " * 20)
    print("WHAT QUERY PLAN INTEGRATION TEST")
    print(" " * 20)

    # Connect to Milvus
    print("\n Connecting to Milvus...")
    if connect_milvus():
        print("    Connected to Milvus")
    else:
        print("    Failed to connect to Milvus")
        return

    # Run tests
    test_query_plan_loading()
    test_answer_plan_loading()
    test_entity_search()
    await test_attribute_resolution()
    test_chunk_search()

    print("\n" + "=" * 60)
    print(" ALL TESTS COMPLETED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
