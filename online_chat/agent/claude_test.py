import instructor
from anthropic import Anthropic, AsyncAnthropic

# Use Claude directly, not instructor
def extract_metadata_with_claude(example_contents=None, filename=None, with_opus=False):
    if example_contents is None:
        from pathlib import Path
        example_contents = Path(__file__).read_text()
        filename = Path(__file__).name
    client = Anthropic()
    model = "claude-3-opus-20240229" if with_opus else "claude-3-haiku-20240307"
    prompt = (
        "Extract the metadata for this example as a JSON object with the following fields: "
        "['summary', 'tags', 'difficulty', 'use_cases', 'freshness'].\n"
        "-----EXAMPLE BEGINS-----\n"
        f"{example_contents}\n"
        "-----EXAMPLE ENDS-----\n"
        "Respond ONLY with the JSON object."
    )
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        temperature=0.0,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )
    import json
    # Find the first code block or JSON object in the response
    import re
    content = response.content[0].text if hasattr(response.content[0], "text") else response.content[0]
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        metadata = json.loads(match.group(0))
    else:
        raise ValueError("No JSON object found in Claude response.")
    # inject the filename
    metadata["filename"] = filename
    # return as JSON string
    return json.dumps(metadata)