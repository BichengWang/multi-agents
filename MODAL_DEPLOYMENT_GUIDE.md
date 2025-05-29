# Modal Deployment Guide

This guide will help you deploy your LLM to Modal and connect it to your chat application.

## Current Status ✅

Your chat application is **working with mock responses**! You can test the interface at:
- **Main app**: http://localhost:8501 (run: `streamlit run chat_app_qwen.py`)
- **Web app**: http://localhost:8501 (run: `streamlit run web/chat_app_qwen.py`)

## Step 1: Set Up Modal Authentication

1. **Create a Modal account** at https://modal.com if you haven't already

2. **Install and authenticate Modal**:
   ```bash
   # Modal is already installed, now authenticate
   modal setup
   ```
   This will open a browser window for authentication.

## Step 2: Deploy Your LLM

1. **Navigate to the serve directory**:
   ```bash
   cd serve
   ```

2. **Deploy the LLM service**:
   ```bash
   modal deploy serve_llm.py
   ```

3. **Copy the deployment URL** from the output. It will look like:
   ```
   https://your-username--qwen-chat-llm-generate-text.modal.run
   ```

## Step 3: Configure Your Chat App

1. **Update `modal_config.py`**:
   ```python
   MODAL_CONFIG = {
       "app_url": "https://your-actual-deployment-url.modal.run",  # Replace with your URL
       "timeout": 30,
       "max_retries": 3
   }
   
   MOCK_CONFIG = {
       "enabled": False,  # Disable mock mode
       # ... rest of config
   }
   ```

2. **Or set environment variable**:
   ```bash
   export MODAL_APP_URL="https://your-actual-deployment-url.modal.run"
   ```

## Step 4: Test Your Deployment

1. **Test the Modal endpoint directly**:
   ```bash
   curl -X POST "https://your-deployment-url.modal.run" \
        -H "Content-Type: application/json" \
        -d '{"prompt": "Hello, how are you?", "max_tokens": 50}'
   ```

2. **Test in the chat app**:
   - The app will automatically detect the real Modal URL
   - Use the "Test Connection" button in the sidebar
   - Try asking questions in the chat

## Troubleshooting

### Common Issues:

1. **405 Method Not Allowed**:
   - Check that your Modal URL is correct
   - Ensure the endpoint accepts POST requests
   - Verify the URL format: `https://username--app-name-function-name.modal.run`

2. **Authentication Errors**:
   - Run `modal setup` again
   - Check your Modal account status

3. **Deployment Fails**:
   - Check your Modal credits/billing
   - Verify the model name in `serve_llm.py`
   - Check GPU availability

### Model Configuration:

The current deployment uses `facebook/opt-125m` for testing. To use a different model:

1. **Edit `serve/serve_llm.py`**:
   ```python
   # Change this line:
   model_id = "facebook/opt-125m"
   # To your preferred model:
   model_id = "Qwen/Qwen2.5-7B-Instruct"  # or any other model
   ```

2. **Redeploy**:
   ```bash
   modal deploy serve_llm.py
   ```

## Advanced Configuration

### Custom Model Parameters:

You can modify the generation parameters in `serve_llm.py`:

```python
result = model_pipeline(
    prompt, 
    max_new_tokens=max_tokens, 
    do_sample=True, 
    temperature=temperature,
    top_p=0.9,  # Add nucleus sampling
    repetition_penalty=1.1,  # Reduce repetition
    pad_token_id=model_pipeline.tokenizer.eos_token_id
)
```

### Scaling Configuration:

Adjust the Modal function parameters for better performance:

```python
@app.function(
    image=image, 
    gpu="A100",  # Use more powerful GPU
    timeout=600,
    container_idle_timeout=300,
    allow_concurrent_inputs=50  # Handle more concurrent requests
)
```

## Cost Optimization

- **GPU Selection**: Start with A10G, upgrade to A100 if needed
- **Idle Timeout**: Adjust `container_idle_timeout` based on usage patterns
- **Model Size**: Use smaller models for testing, larger for production

## Next Steps

1. ✅ **Current**: Chat app working with mock responses
2. 🔄 **Next**: Deploy to Modal following this guide
3. 🚀 **Future**: Add more advanced features like conversation memory, document upload, etc.

## Support

- **Modal Documentation**: https://modal.com/docs
- **Modal Discord**: Join the Modal community for support
- **GitHub Issues**: Report issues with the chat application

---

**Happy chatting! 🤖💬** 