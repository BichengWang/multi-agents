# To run this app, use the command: streamlit run web/chat_app_qwen.py
import streamlit as st
import requests
import os
import json
import sys

# Add the project root to the path so we can import from config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Try to import LlamaIndex components, but handle gracefully if they fail
try:
    from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, Settings
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    LLAMAINDEX_AVAILABLE = True
except ImportError as e:
    st.warning(f"LlamaIndex components not fully available: {e}")
    LLAMAINDEX_AVAILABLE = False

# Import modal config
try:
    from modal_config import MODAL_CONFIG, is_mock_enabled, get_mock_response
except ImportError:
    # Fallback config if modal_config.py doesn't exist
    MODAL_CONFIG = {
        "app_url": "https://your-modal-app-url.modal.run",
        "timeout": 30,
        "max_retries": 3
    }

def call_modal_llm(prompt, max_tokens=512, temperature=0.7):
    """Call the LLM deployed on Modal"""
    try:
        # Check if mock mode is enabled
        if is_mock_enabled():
            st.info("🧪 Using mock responses (Modal not deployed yet)")
            return get_mock_response(prompt)
            
    except ImportError:
        # Fallback if modal_config doesn't have the new functions
        MODAL_CONFIG = {
            "app_url": "https://your-modal-app-url.modal.run",
            "timeout": 30,
            "max_retries": 3
        }
    
    try:
        response = requests.post(
            f"{MODAL_CONFIG['app_url']}/generate",
            json={
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature
            },
            timeout=MODAL_CONFIG.get('timeout', 30)
        )
        response.raise_for_status()
        result = response.json()
        if "response" in result:
            return result["response"]
        elif "error" in result:
            return f"Error from LLM: {result['error']}"
        else:
            return "Unexpected response format from LLM"
    except requests.exceptions.RequestException as e:
        # If Modal request fails, provide helpful error message
        if "405" in str(e) or "Method Not Allowed" in str(e):
            return f"""❌ **Modal Deployment Error**: The URL `{MODAL_CONFIG['app_url']}` is not valid.

**To fix this:**
1. Deploy your LLM to Modal: `cd serve && modal deploy serve_llm.py`
2. Copy the deployment URL from the output
3. Update `modal_config.py` with your actual URL
4. Set `MOCK_CONFIG["enabled"] = False`

**For now, you can enable mock mode in `modal_config.py` to test the interface.**"""
        else:
            return f"Connection error: {str(e)}"
    except json.JSONDecodeError as e:
        return f"JSON decode error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

def load_documents_simple(directory_path):
    """Simple document loading without LlamaIndex"""
    if not os.path.exists(directory_path):
        return None
    
    documents = []
    try:
        for filename in os.listdir(directory_path):
            if filename.endswith(('.txt', '.md')):
                filepath = os.path.join(directory_path, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    documents.append({
                        'filename': filename,
                        'content': content
                    })
        return documents
    except Exception as e:
        st.error(f"Error loading documents: {e}")
        return None

def simple_context_search(documents, query):
    """Simple keyword-based context search"""
    if not documents:
        return ""
    
    query_words = query.lower().split()
    relevant_docs = []
    
    for doc in documents:
        content_lower = doc['content'].lower()
        score = sum(1 for word in query_words if word in content_lower)
        if score > 0:
            relevant_docs.append((doc, score))
    
    # Sort by relevance score
    relevant_docs.sort(key=lambda x: x[1], reverse=True)
    
    # Take top 2 most relevant documents
    context_parts = []
    for doc, score in relevant_docs[:2]:
        context_parts.append(f"From {doc['filename']}:\n{doc['content'][:500]}...")
    
    return "\n\n".join(context_parts)

# Initialize session state
if "index" not in st.session_state:
    st.session_state.index = None
if "simple_documents" not in st.session_state:
    st.session_state.simple_documents = None
if "messages" not in st.session_state:
    st.session_state.messages = []

st.title("Financial Advisor Chatbot with Qwen LLM (Modal)")

# Add a sidebar with connection info
with st.sidebar:
    st.header("Connection Info")
    st.write(f"**Modal App URL:** {MODAL_CONFIG['app_url']}")
    
    # Document loading section
    st.subheader("📁 Document Context")
    doc_directory = st.text_input(
        "Document directory path:",
        value="../documents",
        help="Path to directory containing documents for context"
    )
    
    if st.button("Load Documents"):
        with st.spinner("Loading documents..."):
            if LLAMAINDEX_AVAILABLE:
                try:
                    documents = SimpleDirectoryReader(doc_directory).load_data()
                    if documents:
                        st.session_state.index = VectorStoreIndex.from_documents(documents)
                        st.success(f"Loaded {len(documents)} documents with vector indexing!")
                    else:
                        st.error("No documents found")
                except Exception as e:
                    st.warning(f"Vector indexing failed: {e}. Using simple search.")
                    st.session_state.simple_documents = load_documents_simple(doc_directory)
                    if st.session_state.simple_documents:
                        st.success(f"Loaded {len(st.session_state.simple_documents)} documents with simple search!")
            else:
                st.session_state.simple_documents = load_documents_simple(doc_directory)
                if st.session_state.simple_documents:
                    st.success(f"Loaded {len(st.session_state.simple_documents)} documents with simple search!")
                else:
                    st.error("Failed to load documents")
    
    # Test connection button
    if st.button("Test Connection"):
        with st.spinner("Testing connection to Modal LLM..."):
            test_response = call_modal_llm("Hello, this is a test.", max_tokens=10)
            if "Error" in test_response or "error" in test_response.lower():
                st.error(f"Connection failed: {test_response}")
            else:
                st.success("Connection successful!")
                st.write(f"Response: {test_response[:100]}...")

# Display chat messages from history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Accept user input
if prompt := st.chat_input("Ask your financial question here..."):
    # Add user message to chat history
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Generate response with context if available
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            context = ""
            
            # Try to get context from documents
            if st.session_state.index is not None and LLAMAINDEX_AVAILABLE:
                try:
                    query_engine = st.session_state.index.as_query_engine()
                    rag_response = query_engine.query(prompt)
                    context = str(rag_response)
                except Exception as e:
                    st.warning(f"RAG retrieval failed: {e}")
            elif st.session_state.simple_documents:
                context = simple_context_search(st.session_state.simple_documents, prompt)

            # Prepare the full prompt with system instructions and context
            system_prompt = (
                "You are a helpful and knowledgeable financial advisor. "
                "Provide clear, accurate, and actionable financial advice. "
                "If you are unsure or the question is outside your expertise, "
                "politely let the user know."
            )
            
            # Combine system prompt, context, and user question
            if context:
                full_prompt = f"{system_prompt}\n\nContext: {context}\n\nUser Question: {prompt}\n\nResponse:"
            else:
                full_prompt = f"{system_prompt}\n\nUser Question: {prompt}\n\nResponse:"

            assistant_reply = call_modal_llm(full_prompt)
            
            if assistant_reply:
                st.markdown(assistant_reply)
                # Add assistant response to chat history
                st.session_state.messages.append({"role": "assistant", "content": assistant_reply})
            else:
                st.error("Failed to get response from the model")

# Add a clear chat button
if st.button("Clear Chat History"):
    st.session_state.messages = []
    st.rerun()

# Display some usage instructions
with st.expander("Usage Instructions"):
    st.markdown("""
    ### How to use this chat app:
    
    1. **Load Documents** (optional): Use the sidebar to load documents for context
    2. **Ask financial questions** in the chat input below
    3. The app will use **RAG (Retrieval-Augmented Generation)** to find relevant context from documents
    4. Your **Modal-deployed Qwen LLM** will generate responses based on the context
    5. Use the **Test Connection** button in the sidebar to verify the Modal LLM is working
    6. Use **Clear Chat History** to start a new conversation
    
    ### Features:
    - 🤖 **Custom LLM**: Uses your Modal-deployed model instead of OpenAI
    - 📚 **RAG Integration**: Retrieves relevant context from your documents
    - 💬 **Chat History**: Maintains conversation context
    - 🔗 **Connection Testing**: Verify your Modal deployment is working
    - 🛡️ **Graceful Fallbacks**: Works even if some components are unavailable
    """) 