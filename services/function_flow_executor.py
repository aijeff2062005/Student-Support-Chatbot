import importlib
import importlib.util
import logging
import traceback
from typing import Any

import yaml

from utils.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


class FunctionFlowExecutor:
    def __init__(self):
        self.state = {}
        self.input_data = {}

    def load_flow_config(self, flow_file_path: str) -> dict[str, Any]:
        """Load YAML configuration."""
        with open(flow_file_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _get_value_from_state(self, path: str):
        """
        Extract value from state/input using dot notation.
        Supports:
        - $input.key
        - $state.key
        - [index]
        - [*] for list mapping
        """
        if not isinstance(path, str) or not path.startswith("$"):
            return path

        # Determine root
        if path.startswith("$input."):
            current = self.input_data
            clean_path = path[7:]
        elif path.startswith("$state."):
            current = self.state
            clean_path = path[7:]
        else:
            return path

        # Split path, handling keys and indices
        # Simple parser: split by '.' then handle [i]
        # This is a basic implementation. For complex paths might need library.

        # Using pydash if available would be easier, but let's implement basic
        # logic to avoid dependency issues if not installed.
        # Actually pydash.get handles data.key[0].item nicely.

        # If path has [*], we need to map over the list.
        # e.g. resolved_attributes[*].attribute_key
        if "[*]" in clean_path:
            parts = clean_path.split("[*].")
            list_path = parts[0]
            item_path = parts[1] if len(parts) > 1 else None

            # Get the list
            data_list = self._get_simple_path(current, list_path)
            if not isinstance(data_list, list):
                return []

            if item_path:
                return [self._get_simple_path(item, item_path) for item in data_list]
            return data_list

        return self._get_simple_path(current, clean_path)

    def _get_simple_path(self, data, path):
        """Resolve path like a.b[0].c"""
        # Hacky parse: preserve full path for now?
        # Python doesn't support a.b[0] in dict access directly easily without parsing.
        # Let's try to use split and recursive lookup.

        # Replace [i] with .i for easier splitting if keys don't have dots
        # This is fragile but works for known controlled inputs.
        # attributes[0] -> attributes.0

        # Better: use a loop
        # Split by '.'
        # For each part, check if it has [index]
        if not path:
            return data

        keys = path.split(".")
        current = data

        for key in keys:
            if current is None:
                return None

            # Check for list index like "entities[0]"
            if "[" in key and key.endswith("]"):
                # Extract name and index name[index]
                k_part, idx_part = key[:-1].split("[")
                if k_part:
                    if isinstance(current, dict):
                        current = current.get(k_part)
                    else:
                        try:
                            current = getattr(current, k_part)
                        except AttributeError:
                            return None

                try:
                    idx = int(idx_part)
                    if isinstance(current, list) and 0 <= idx < len(current):
                        current = current[idx]
                    else:
                        return None
                except ValueError:
                    # String index? (Dict)
                    if isinstance(current, dict):
                        current = current.get(idx_part)
                    else:
                        return None
            else:
                # Try numeric index for lists (e.g. found_entity_list.0.node_id)
                if isinstance(current, list):
                    try:
                        idx = int(key)
                        if 0 <= idx < len(current):
                            current = current[idx]
                        else:
                            return None
                        continue
                    except ValueError:
                        return None
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    try:
                        current = getattr(current, key)
                    except AttributeError:
                        return None
        return current

    def _evaluate_condition(self, condition_str: str) -> bool:
        """
        Evaluate python-like condition string.
        WARNING: Uses eval(). Only safe for controlled inputs.
        """
        if not condition_str:
            return True

        # We need to expose state values for evaluation
        # We can parse the string and replace $variables with values
        # OR we can just pass a context dict to eval, but keys need to match

        # Replace $input.x and $state.y with values
        # Because eval needs valid python syntax, we can't just dump $var
        # We'll replace known patterns with `self._get_value_from_state('$...')` result?
        # No, that's hard to inject into eval string.

        # Alternative: We manually extract variables we support in condition
        # "len($state.resolved_attributes) == 0"

        # Let's regex replace $path with self._get_value_from_state('$path')
        # But we need to handle the value being a list/dict object.

        # Better approach:
        # Resolve variables first, then put them in scope.
        # But variables are embedded in string.

        # Simple hack for this specific task:
        # replace $state.x with state_x
        # extract value of $state.x into state_x variable
        # run eval.

        import re

        # Find all $ patterns: $state.foo or $input.bar or $state.a.b or $state.list.0.key
        # [\w]+ after dot allows numeric segments like .0 for list index access
        matches = re.findall(r"\$[a-zA-Z_]\w*(?:\.[\w]+)*(?:\[\d+])*", condition_str)

        # Deduplicate and sort longest-first so $state.foo.0.bar is replaced
        # before $state.foo (prevents partial replacement corruption)
        unique_matches = list(dict.fromkeys(matches))
        unique_matches.sort(key=len, reverse=True)

        eval_context = {}
        processed_condition = condition_str

        for i, match in enumerate(unique_matches):
            val = self._get_value_from_state(match)
            var_name = f"var_{i}"
            eval_context[var_name] = val
            processed_condition = processed_condition.replace(match, var_name)

        try:
            safe_builtins = {
                "len": len,
                "any": any,
                "all": all,
                "str": str,
                "int": int,
                "bool": bool,
                "None": None,
                "True": True,
                "False": False,
                "true": True,
                "false": False,
            }
            return eval(processed_condition, {"__builtins__": safe_builtins}, eval_context)
        except Exception as e:
            traceback.print_exc()
            logger.error(f"Error evaluating condition '{condition_str}': {e}")
            return False

    def execute_flow(self, flow_file_path: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Execute the workflow."""
        self.input_data = input_data
        self.state = {}

        config = self.load_flow_config(flow_file_path)
        steps = config.get("steps", [])
        # logger.error(f"Type of Input data: {type(input_data)}")
        logger.debug(f"Input data: {input_data}")

        logger.info(f"Starting flow: {config.get('flow_name')}")

        for step in steps:
            step_id = step.get("step_id")
            name = step.get("name")

            # Check condition
            condition = step.get("condition")
            if condition:
                if not self._evaluate_condition(condition):
                    logger.info(f"Skipping step {step_id}:{name} (Condition met: False)")
                    continue

            logger.info(f"Executing step {step_id}:{name}")

            try:
                # Prepare Inputs
                inputs_config = step.get("inputs", {})
                func_args = {}
                for arg_name, arg_val in inputs_config.items():
                    # Check if arg_val is a list of paths (e.g. entities[$])
                    # or just a value/path
                    if isinstance(arg_val, list):
                        func_args[arg_name] = [self._get_value_from_state(v) for v in arg_val]
                    else:
                        func_args[arg_name] = self._get_value_from_state(arg_val)

                # Load Module & Function
                module_name = step.get("module")
                module = importlib.import_module(module_name)

                func_name = step.get("function")
                class_name = step.get("class")
                method_name = step.get("method")

                result = None

                if class_name and method_name:
                    # Instantiate class and call method
                    cls = getattr(module, class_name)
                    # Assumption: class init takes no args or we don't config them here
                    instance = cls()
                    method = getattr(instance, method_name)
                    result = method(**func_args)
                elif func_name:
                    # Static function
                    func = getattr(module, func_name)

                    import asyncio

                    if asyncio.iscoroutinefunction(func):
                        # We need to run await.
                        # But current execute_flow is sync.
                        # This creates issue if called from sync context.
                        # For now, let's assume we run inside async loop?
                        # Or use asyncio.run if this is top level entrance.
                        # If I make execute_flow async, it solves it.
                        pass  # will handle in async wrapper below
                    else:
                        result = func(**func_args)

                # Check for async result (if it returned a coroutine but wasn't caught above)
                if importlib.util.find_spec("asyncio"):
                    import asyncio

                    if asyncio.iscoroutine(result):
                        # If we are in an async loop, we should await.
                        # Since I cannot easily detect/await here without being async,
                        # I should make execute_flow async.
                        pass

                # Store output
                output_key = step.get("outputs")
                if output_key:
                    self.state[output_key] = result
                    logger.info(f"Step {name} finished. Output stored in {output_key}.")

            except Exception as e:
                logger.error(f"Error in step {name}: {e}")
                import traceback

                traceback.print_exc()
                # Continue or break? Usually break on error
                # break

        return self.state

    async def execute_flow_async(self, flow_file_path: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Async version of execute_flow"""
        self.input_data = input_data
        self.state = {}

        config = self.load_flow_config(flow_file_path)
        steps = config.get("steps", [])

        logger.info(f"Starting flow (Async): {config.get('flow_name')}")

        for step in steps:
            step_id = step.get("step_id")
            name = step.get("name")
            logger.info(f"Executing step {step_id}:{name}")

            # Check condition
            condition = step.get("condition")
            if condition:
                if not self._evaluate_condition(condition):
                    logger.info(f"Skipping step {step_id}:{name} (Condition met: False)")
                    continue

            logger.info(f"Executing step {step_id}:{name}")

            try:
                # Prepare Inputs
                inputs_config = step.get("inputs", {})
                func_args = {}
                for arg_name, arg_val in inputs_config.items():
                    if isinstance(arg_val, list):
                        func_args[arg_name] = [self._get_value_from_state(v) for v in arg_val]
                    else:
                        func_args[arg_name] = self._get_value_from_state(arg_val)

                # Load Module & Function
                module_name = step.get("module")
                module = importlib.import_module(module_name)

                func_name = step.get("function")
                class_name = step.get("class")
                method_name = step.get("method")

                result = None

                if class_name and method_name:
                    cls = getattr(module, class_name)
                    instance = cls()
                    method = getattr(instance, method_name)
                    if importlib.util.find_spec("asyncio"):
                        import asyncio

                        if asyncio.iscoroutinefunction(method):
                            result = await method(**func_args)
                        else:
                            result = method(**func_args)
                    else:
                        result = method(**func_args)

                elif func_name:
                    func = getattr(module, func_name)
                    import asyncio

                    if asyncio.iscoroutinefunction(func):
                        result = await func(**func_args)
                    else:
                        result = func(**func_args)

                # Store output
                output_key = step.get("outputs")
                if output_key:
                    self.state[output_key] = result
                    state_keys = list(self.state.values())
                    logger.info(f"State updated: {state_keys}")

            except Exception as e:
                logger.error(f"Error in step {name}: {e}")
                import traceback

                traceback.print_exc()

        return self.state
