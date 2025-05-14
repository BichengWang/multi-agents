from eval.evaluator import EvaluatorAgent, SyntheticDataConfig


def test_invalid_cases():
    """Test various invalid scenarios for the EvaluatorAgent."""
    print("\n=== Testing Invalid Cases ===")
    
    # Test 1: Invalid format type
    print("\nTest 1: Invalid format type")
    try:
        evaluator = EvaluatorAgent()
        evaluator.generate_data("invalid_format")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 2: Invalid configuration - negative number of items
    print("\nTest 2: Invalid configuration - negative number of items")
    try:
        config = SyntheticDataConfig(num_items=-5)
        evaluator = EvaluatorAgent(config)
        evaluator.generate_data("json")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 3: Invalid configuration - min_value greater than max_value
    print("\nTest 3: Invalid configuration - min_value greater than max_value")
    try:
        config = SyntheticDataConfig(min_value=100, max_value=50)
        evaluator = EvaluatorAgent(config)
        evaluator.generate_data("json")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 4: Empty data generation
    print("\nTest 4: Empty data generation")
    try:
        config = SyntheticDataConfig(num_items=0)
        evaluator = EvaluatorAgent(config)
        result = evaluator.generate_data("json")
        print(f"Empty data result: {result}")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 5: Very large number of items
    print("\nTest 5: Very large number of items")
    try:
        config = SyntheticDataConfig(num_items=1000000)
        evaluator = EvaluatorAgent(config)
        evaluator.generate_data("json")
    except ValueError as e:
        print(f"Expected error: {e}")
    
    # Test 6: Invalid HTML content
    print("\nTest 6: Invalid HTML content")
    try:
        config = SyntheticDataConfig(num_items=1)
        evaluator = EvaluatorAgent(config)
        # Force an invalid HTML character
        evaluator._generate_random_string = lambda length: "<script>alert('xss')</script>"
        result = evaluator.generate_data("html")
        print(f"HTML with special characters: {result}")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 7: Invalid format type (non-string)
    print("\nTest 7: Invalid format type (non-string)")
    try:
        evaluator = EvaluatorAgent()
        evaluator.generate_data(123)  # type: ignore
    except TypeError as e:
        print(f"Expected error: {e}")


def generate_positive_examples():
    print("=== Running Valid Test Cases ===")
    config = SyntheticDataConfig(
        num_items=5,
        min_value=1,
        max_value=1000,
        include_timestamp=True,
        include_id=True
    )
    evaluator = EvaluatorAgent(config)
    print("\nJSON Output:")
    print(evaluator.generate_data("json"))
    print("\nYAML Output:")
    print(evaluator.generate_data("yaml"))
    print("\nHTML Output:")
    print(evaluator.generate_data("html"))
    print("\nPython Output:")
    print(evaluator.generate_data("python"))


if __name__ == "__main__":
    generate_positive_examples()
    test_invalid_cases() 