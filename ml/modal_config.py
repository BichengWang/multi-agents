# Modal Configuration
# Update this URL with your actual Modal app URL once deployed

import os

MODAL_CONFIG = {
    # Your Modal app ID: ap-MpAQH9mDyzSiBsYhOuL19T
    # Dashboard URL: https://modal.com/apps/ey10923/main/ap-MpAQH9mDyzSiBsYhOuL19T
    # 
    # ACTUAL DEPLOYED ENDPOINT URL:
    "app_url": os.getenv("MODAL_APP_URL", "https://ey10923--qwen-chat.modal.run"),
    "timeout": 30,
    "max_retries": 3
}

# Mock server configuration for testing (when Modal is not available)
MOCK_CONFIG = {
    "enabled": False,  # Disabled - using real Modal deployment
    "responses": [
        "I'm a mock response from the LLM. This is just for testing the chat interface.",
        "This is another mock response. Replace this with your actual Modal deployment URL.",
        "Mock LLM: I can help you test the chat application before deploying to Modal.",
        "Test response: The chat interface is working correctly!"
    ]
}

def get_app_url():
    """Get the Modal app URL"""
    return MODAL_CONFIG["app_url"]

def is_mock_enabled():
    """Check if mock mode is enabled"""
    return MOCK_CONFIG.get("enabled", False)

def get_mock_response(prompt):
    """Get a mock response for testing"""
    import random
    responses = MOCK_CONFIG.get("responses", ["Mock response"])
    return random.choice(responses)

# Instructions for finding your Modal endpoint URL:
"""
To find your actual Modal endpoint URL:

1. Go to your Modal dashboard: https://modal.com/apps/ey10923/main/ap-MpAQH9mDyzSiBsYhOuL19T

2. Look for the "Web endpoints" section or "Functions" tab

3. Find your function (likely named "generate-text" or similar)

4. Copy the endpoint URL - it should look like:
   https://ey10923--qwen-chat-llm-generate-text.modal.run
   or
   https://ey10923--your-app-name-function-name.modal.run

5. Update MODAL_CONFIG["app_url"] above with the correct URL

6. Or set environment variable:
   export MODAL_APP_URL="https://your-actual-endpoint-url.modal.run"

Common URL patterns:
- https://username--app-name-function-name.modal.run
- https://username--app-name-function-label.modal.run
"""

# Instructions for setting up Modal deployment:
"""
To deploy your LLM to Modal:

1. Set up Modal authentication:
   modal setup

2. Deploy your app:
   cd serve
   modal deploy serve_llm.py

3. Copy the deployment URL (it will look like):
   https://your-username--qwen-chat-llm-generate-text.modal.run

4. Update this file:
   - Set MODAL_CONFIG["app_url"] to your actual URL
   - Set MOCK_CONFIG["enabled"] to False

5. Or set environment variable:
   export MODAL_APP_URL="https://your-actual-modal-url.modal.run"
""" 