# Qwen Chat Application

A Streamlit-based chat application that interfaces with a Qwen language model deployed on Modal. The app supports both direct chat and document-enhanced conversations using RAG (Retrieval-Augmented Generation).

## Features

- 🤖 **Direct Chat**: Chat directly with the Qwen model deployed on Modal
- 📁 **Document Context**: Upload documents to provide context for more informed responses
- 🎛️ **Configurable Parameters**: Adjust temperature and max tokens
- 📡 **Connection Testing**: Test connectivity to your Modal deployment
- 💬 **Chat History**: Persistent chat history during the session

## Setup

### Prerequisites

1. Python 3.8+ with virtual environment
2. Modal account and deployed Qwen model
3. Required dependencies installed

### Installation

1. Install dependencies:
```bash
pip install streamlit requests llama-index llama-index-embeddings-huggingface
```

2. Configure Modal URL:
   - Edit `modal_config.py` and update the `app_url` with your actual Modal deployment URL
   - Or set the `MODAL_APP_URL` environment variable

### Configuration

Update `modal_config.py` with your Modal deployment details:

```python
MODAL_CONFIG = {
    "app_url": "https://your-actual-modal-app-url.modal.run",
    "timeout": 30,
    "max_retries": 3
}
```

## Usage

### Running the Application

```bash
streamlit run chat_app_qwen.py
```

The app will be available at `http://localhost:8501`

### Basic Chat

1. Open the application in your browser
2. Type your message in the chat input
3. The model will respond using the Qwen LLM deployed on Modal

### Document-Enhanced Chat

1. Check "Use document context" in the sidebar
2. Specify the path to your documents directory (default: `./documents`)
3. Click "Load Documents" to index your documents
4. Ask questions that can be answered using the document context
5. The app will retrieve relevant information and provide context-aware responses

### Model Parameters

Adjust these parameters in the sidebar:
- **Max Tokens**: Maximum length of the response (50-2048)
- **Temperature**: Controls randomness (0.0-2.0, lower = more focused)

## Document Format Support

The application supports various document formats through LlamaIndex:
- Text files (.txt)
- Markdown files (.md)
- PDF files (.pdf)
- Word documents (.docx)
- And more...

## Sample Documents

The `documents/` directory contains sample files for testing:
- `sample_doc.txt`: Overview of multi-agent systems
- `modal_deployment.txt`: Guide to Modal deployment

## API Endpoints

The application expects your Modal deployment to expose a `/generate` endpoint that accepts:

```json
{
    "prompt": "Your prompt here",
    "max_tokens": 512,
    "temperature": 0.7
}
```

And returns:

```json
{
    "response": "Generated response from the model"
}
```

## Troubleshooting

### Connection Issues
- Verify your Modal app URL is correct
- Check that your Modal deployment is running
- Use the "Test Connection" button to verify connectivity

### Document Loading Issues
- Ensure the document directory exists and contains readable files
- Check file permissions
- Verify supported file formats

### Import Errors
- Ensure all dependencies are installed in your virtual environment
- Check Python version compatibility

## Architecture

```
User Input → Streamlit UI → Document Retrieval (optional) → Modal API → Qwen Model → Response
```

1. **User Interface**: Streamlit provides the chat interface
2. **Document Processing**: LlamaIndex handles document loading and vector indexing
3. **Retrieval**: Semantic search finds relevant document chunks
4. **API Call**: HTTP request to Modal deployment
5. **Model Inference**: Qwen model generates response
6. **Display**: Response shown in chat interface

## Dependencies

- `streamlit`: Web application framework
- `requests`: HTTP client for Modal API calls
- `llama-index`: Document processing and RAG
- `llama-index-embeddings-huggingface`: Embedding model for document similarity

## License

This project is part of the multi-agents workspace and follows the same licensing terms. 