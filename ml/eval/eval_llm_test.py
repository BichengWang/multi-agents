from openai import OpenAI
import os
import json

api_key = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=api_key)


def quick_openai_llm_eval(prompt, model="gpt-3.5-turbo"):
    """
    Quickly evaluate an OpenAI LLM by sending a prompt and returning the response.
    If the prompt asks for a JSON format, check if the response is valid JSON.
    Returns a dict with the response and a flag indicating JSON validity (if applicable).
    """
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.7,
        )
        content = response.choices[0].message.content.strip()
        result = {"response": content}

        # Check if the prompt asks for JSON format
        if "json" in prompt.lower():
            try:
                # Try to parse the response as JSON
                json.loads(content)
                result["is_json"] = True
            except Exception:
                result["is_json"] = False
        return result
    except Exception as e:
        return {"response": f"Error during evaluation: {e}", "is_json": False}


# Example usage:
if __name__ == "__main__":
    test_prompt = "Return a summary of the theory of relativity in JSON format."
    result = quick_openai_llm_eval(test_prompt)
    print("LLM Response:", result["response"])
    if "is_json" in result:
        print("Is valid JSON:", result["is_json"])
