#!/usr/bin/env python3
"""
Interactive test script for WHAT definition and attributes query plans.
Flow for WHAT-ATTRIBUTES:
1. Resolve entity → get node_id and attribute_keys
2. Semantic match keyword_attribute → available attribute_keys
3. Execute traversal step from what_attributes.yaml with resolved attributes

Run: python tests/test_what_interactive.py
"""

import asyncio
import sys
from pathlib import Path

import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dbs.milvus_helper import connect_milvus, search_entity_by_name, semantic_resolve_attributes

# Load query plan YAML
QUERY_PLAN_DIR = Path(__file__).parent.parent / "services" / "query_plan"


def load_query_plan(plan_name: str) -> dict:
    """Load query plan YAML file."""
    plan_path = QUERY_PLAN_DIR / f"{plan_name}.yaml"
    if not plan_path.exists():
        raise FileNotFoundError(f"Query plan not found: {plan_path}")

    with open(plan_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_definition(entity_name: str, entity_type: str = "Major"):
    """Test WHAT-DEFINITION: Tìm entity và lấy description."""
    print(f"\n{'=' * 60}")
    print(f" WHAT-DEFINITION: '{entity_name}' là gì?")
    print(f"{'=' * 60}")

    results = search_entity_by_name(entity_name=[entity_name], entity_type=[entity_type], threshold=0.5, top_k=3)

    if results:
        for i, r in enumerate(results, 1):
            print(f"\n Kết quả {i}:")
            print(f"   Tên: {r['node_name']}")
            print(f"   Loại: {r['node_type']}")
            print(f"   Score: {r['score']:.3f}")
            desc = r.get("description", "N/A")
            if desc and len(desc) > 200:
                desc = desc[:200] + "..."
            print(f"   Description: {desc}")
            print(f"   Attributes: {list(r.get('attribute_keys', []))[:5]}")
    else:
        print("    Không tìm thấy entity")

    return results[0] if results else None


async def test_attributes(
    entity_name: str, entity_type: str, keyword_attributes: list[str], subtopics: list[str] = None
):
    """
    Test WHAT-ATTRIBUTES flow:
    1. Resolve entity → get node_id and attribute_keys
    2. Semantic match keyword_attribute → available attribute_keys
    3. Execute appropriate traversal step based on subtopics
    """
    print(f"\n{'=' * 60}")
    print(f" WHAT-ATTRIBUTES: '{entity_name}' - {keyword_attributes}")
    print(f"   Subtopics: {subtopics or []}")
    print(f"{'=' * 60}")

    # ============================================
    # STEP 1: Resolve entity to get node_id and attribute_keys
    # ============================================
    print("\n Step 1: Entity Resolution")

    entity_results = search_entity_by_name(entity_name=[entity_name], entity_type=[entity_type], threshold=0.5, top_k=1)

    if not entity_results:
        print("    Entity không tìm thấy")
        return None

    entity = entity_results[0]
    node_id = entity["node_id"]
    node_name = entity["node_name"]
    node_type = entity["node_type"]
    attribute_keys = list(entity.get("attribute_keys", []))

    print(f"    Found: {node_name} (ID: {node_id})")
    print(f"    Available attributes: {attribute_keys[:8]}...")

    # ============================================
    # ============================================
    print("\n Step 2: Semantic Attribute Resolution")
    print(f"   Input keyword_attributes: {keyword_attributes}")

    resolved_attrs = await semantic_resolve_attributes(
        requested_attributes=keyword_attributes,
        available_attribute_keys=attribute_keys,
        similarity_threshold=0.5,
        top_k=3,
    )

    if resolved_attrs:
        print("    Resolved attributes:")
        for attr in resolved_attrs:
            print(f"      - {attr['attribute_key']} (score: {attr['score']:.3f})")
    else:
        print("   ️ Không resolve được attributes từ Milvus, fallback...")

    # ============================================
    # STEP 3: Load query plan and determine traversal step
    # ============================================
    print("\n Step 3: Load Query Plan & Execute")

    try:
        query_plan = load_query_plan("what_attributes")
        print(f"    Loaded: {query_plan['flow_name']}")

        # Find matching traversal step based on subtopics
        matching_steps = []
        for step in query_plan.get("traversal_steps", []):
            step_name = step.get("name")
            condition_subtopics = step.get("condition_subtopics", [])

            # Step 1 (main_entity) always runs
            if step.get("step") == 1:
                matching_steps.append(step)
                continue

            # Check if subtopics match condition
            if subtopics and condition_subtopics:
                if any(sub in condition_subtopics for sub in subtopics):
                    matching_steps.append(step)

        print(f"    Matching steps: {[s['name'] for s in matching_steps]}")

        # Show Cypher templates that would execute
        for step in matching_steps:
            step_name = step.get("name")
            cypher = step.get("cypher_template", "").strip()
            attrs = step.get("attributes", []) or step.get("attributes_by_type", {}).get(node_type, [])

            print(f"\n    Step: {step_name}")
            print(f"      Attributes to fetch: {attrs}")

            # Replace placeholders in cypher template
            cypher_filled = cypher.replace("{entity_type}", node_type)
            cypher_filled = cypher_filled.replace("$entity_id", f'"{node_id}"')
            print(f"      Cypher: {cypher_filled[:150]}...")

    except FileNotFoundError as e:
        print(f"    {e}")

    # ============================================
    # Summary
    # ============================================
    print(f"\n{'=' * 60}")
    print(" SUMMARY")
    print(f"{'=' * 60}")
    print(f"   Entity: {node_name} ({node_type})")
    print(f"   Node ID: {node_id}")
    print(f"   Requested: {keyword_attributes}")
    if resolved_attrs:
        print(f"   Resolved to: {[a['attribute_key'] for a in resolved_attrs]}")
    print(f"   Subtopics: {subtopics or 'None'}")
    print(f"   Steps to execute: {[s['name'] for s in matching_steps]}")

    return {"entity": entity, "resolved_attrs": resolved_attrs, "matching_steps": matching_steps}


async def main():
    print("\n" + " " * 15)
    print("WHAT QUERY PLAN INTERACTIVE TEST")
    print(" " * 15)

    # Connect to Milvus
    print("\n Connecting to Milvus...")
    if not connect_milvus():
        print("    Failed to connect to Milvus")
        return
    print("    Connected!")

    # ============================================
    # TEST 1: WHAT-DEFINITION
    # ============================================
    print("\n" + "=" * 60)
    print("TEST 1: WHAT-DEFINITION")
    print("=" * 60)

    test_definition("Ngành CNTT", "Major")
    test_definition("Khoa Công nghệ thông tin", "Faculty")

    # ============================================
    # TEST 2: WHAT-ATTRIBUTES
    # ============================================
    print("\n" + "=" * 60)
    print("TEST 2: WHAT-ATTRIBUTES")
    print("=" * 60)

    await test_attributes(
        entity_name="Ngành Marketing",
        entity_type="Major",
        keyword_attributes=["học phí", "fee_information"],
        subtopics=["cost_fee"],
    )

    await test_attributes(
        entity_name="Ngành CNTT",
        entity_type="Major",
        keyword_attributes=["điểm chuẩn", "cutoff_score"],
        subtopics=["cutoff"],
    )

    await test_attributes(
        entity_name="Ngành Kế toán", entity_type="Major", keyword_attributes=["chỉ tiêu", "quota"], subtopics=["quota"]
    )

    await test_attributes(
        entity_name="Ngành CNTT",
        entity_type="Major",
        keyword_attributes=["tỷ lệ tốt nghiệp", "graduation_rate"],
        subtopics=[],
    )

    # ============================================
    # TEST 3: INTERMEDIATE NODE TRAVERSAL
    # ============================================
    print("\n" + "=" * 60)
    print("TEST 3: INTERMEDIATE NODE TRAVERSAL")
    print("=" * 60)

    await test_intermediate_node(source_entity="Ngành CNTT", source_type="Major", target_type="University")

    await test_intermediate_node(source_entity="Khoa Công nghệ thông tin", source_type="Faculty", target_type="Campus")

    print("\n" + "=" * 60)
    print(" ALL TESTS COMPLETED!")
    print("=" * 60)


async def test_intermediate_node(source_entity: str, source_type: str, target_type: str):
    """
    Test intermediate node traversal.
    When needs_intermediate_node=true, find path between entities that are NOT directly connected.
    """
    print(f"\n{'=' * 60}")
    print(f" INTERMEDIATE NODE: {source_type} → ? → {target_type}")
    print(f"   Source: '{source_entity}'")
    print(f"{'=' * 60}")

    # Step 1: Resolve source entity
    print("\n Step 1: Resolve Source Entity")

    entity_results = search_entity_by_name(
        entity_name=[source_entity], entity_type=[source_type], threshold=0.5, top_k=1
    )

    if not entity_results:
        print("    Source entity not found")
        return None

    entity = entity_results[0]
    node_id = entity["node_id"]
    node_name = entity["node_name"]

    print(f"    Found: {node_name} (ID: {node_id})")

    # Step 2: Load query plan and find intermediate path
    print("\n Step 2: Find Intermediate Path")

    query_plan = load_query_plan("what_attributes")
    intermediate_paths = query_plan.get("intermediate_node_paths", {})

    # Look for matching path
    path_key = f"{source_type}_to_{target_type}"

    if path_key in intermediate_paths:
        path_config = intermediate_paths[path_key]
        print(f"    Found path: {path_config['path']}")

        # Show Cypher that would be executed
        cypher = path_config["cypher_template"].strip()
        cypher_filled = cypher.replace("$entity_id", f'"{node_id}"')

        print("\n Cypher Query:")
        print(f"   {cypher_filled}")

        return {
            "source": {"name": node_name, "type": source_type, "id": node_id},
            "target_type": target_type,
            "path": path_config["path"],
            "cypher": cypher_filled,
        }
    else:
        # Check reverse path
        reverse_key = f"{target_type}_to_{source_type}"
        if reverse_key in intermediate_paths:
            path_config = intermediate_paths[reverse_key]
            print(f"   ️ Found reverse path: {path_config['path']}")
            print("   ️ Would need to reverse the query direction")
        else:
            print(f"    No intermediate path defined for {path_key}")
            print(f"   ℹ️ Available paths: {list(intermediate_paths.keys())}")

    return None


if __name__ == "__main__":
    asyncio.run(main())
