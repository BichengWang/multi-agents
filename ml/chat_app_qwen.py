import streamlit as st
import requests
import json
from modal_config import MODAL_CONFIG, is_mock_enabled, get_mock_response
import os

# Try to import LlamaIndex components, but handle gracefully if they fail
try:
    from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, Settings
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    LLAMAINDEX_AVAILABLE = True
except ImportError as e:
    st.warning(f"LlamaIndex components not fully available: {e}")
    LLAMAINDEX_AVAILABLE = False

def call_modal_llm(prompt, max_tokens=512, temperature=0.7):
    """Call the LLM deployed on Modal"""
    # Check if mock mode is enabled
    if is_mock_enabled():
        st.info("🧪 Using mock responses (Modal not deployed yet)")
        return get_mock_response(prompt)
    
    try:
        response = requests.post(
            MODAL_CONFIG['app_url'],  # Remove /generate path - Modal endpoint is at root
            json={
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature
            },
            timeout=30
        )
        response.raise_for_status()
        return response.json()["response"]
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
            st.error(f"Error calling Modal LLM: {e}")
            return None

def load_documents(directory_path):
    """Load documents from a directory using LlamaIndex"""
    if not LLAMAINDEX_AVAILABLE:
        st.error("LlamaIndex not available for document processing")
        return None
        
    if not os.path.exists(directory_path):
        return None
    
    try:
        documents = SimpleDirectoryReader(directory_path).load_data()
        return documents
    except Exception as e:
        st.error(f"Error loading documents: {e}")
        return None

def create_index(documents):
    """Create a vector index from documents"""
    if not LLAMAINDEX_AVAILABLE:
        st.error("LlamaIndex not available for indexing")
        return None
        
    try:
        # Try with default embedding first
        index = VectorStoreIndex.from_documents(documents)
        return index
    except Exception as e:
        st.error(f"Error creating index: {e}")
        return None

def query_with_context(index, query, llm_response_func):
    """Query the index and get context for the LLM"""
    if not LLAMAINDEX_AVAILABLE or index is None:
        return llm_response_func(query)
        
    try:
        query_engine = index.as_query_engine(
            response_mode="compact",
            similarity_top_k=3
        )
        
        # Get relevant context
        context_response = query_engine.query(query)
        context = str(context_response)
        
        # Create enhanced prompt with context for mental health support
        enhanced_prompt = f"""You are a knowledgeable mental health customer service assistant. Use the following context to provide helpful, empathetic, and professional responses.

Context: {context}

Customer Question: {query}

Please provide a compassionate and informative response based on the context. If the context doesn't contain relevant information, provide general mental health support guidance while being clear about your limitations."""
        
        # Get response from Modal LLM
        return llm_response_func(enhanced_prompt)
    except Exception as e:
        st.error(f"Error querying with context: {e}")
        return llm_response_func(query)

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

def get_sample_questions():
    """Get sample mental health customer service questions"""
    return [
        "How should I handle a customer experiencing anxiety?",
        "What are effective active listening techniques?",
        "How can I show empathy to distressed customers?",
        "What is trauma-informed care?",
        "How do I maintain professional boundaries?",
        "What should I do if a customer mentions self-harm?",
        "How can I de-escalate a tense situation?",
        "What are signs of depression I should recognize?",
        "How do I refer customers to mental health resources?",
        "What self-care practices should I follow?"
    ]

def main():
    st.title("🧠💙 HarborSoul Chat Companion")
    st.markdown("AI-powered support for mental health customer service representatives")
    
    # Add helpful introduction
    with st.expander("ℹ️ About this Assistant", expanded=False):
        st.markdown("""
        This assistant is designed to help customer service representatives in mental health settings. 
        It provides guidance on:
        
        - **Active listening techniques**
        - **Empathy and communication skills**
        - **Trauma-informed care principles**
        - **Crisis intervention basics**
        - **Professional boundaries**
        - **Self-care strategies**
        
        **Important**: This tool provides general guidance only. Always follow your organization's 
        protocols and seek supervision for complex situations.
        """)
    
    # Sidebar for configuration
    with st.sidebar:
        st.header("⚙️ Configuration")
        
        # Document upload section
        st.subheader("📚 Knowledge Base")
        use_documents = st.checkbox("Use mental health knowledge base", value=True)
        
        if use_documents:
            doc_directory = st.text_input(
                "Knowledge base directory:",
                value="./docs",
                help="Path to directory containing mental health documentation"
            )
            
            if st.button("Load Knowledge Base"):
                with st.spinner("Loading mental health documentation..."):
                    if LLAMAINDEX_AVAILABLE:
                        documents = load_documents(doc_directory)
                        if documents:
                            st.session_state.documents = documents
                            st.session_state.index = create_index(documents)
                            if st.session_state.index:
                                st.success(f"✅ Loaded {len(documents)} documents with AI indexing!")
                                st.session_state.use_simple_search = False
                            else:
                                st.warning("AI indexing failed, using keyword search")
                                st.session_state.simple_documents = load_documents_simple(doc_directory)
                                st.session_state.use_simple_search = True
                                if st.session_state.simple_documents:
                                    st.success(f"✅ Loaded {len(st.session_state.simple_documents)} documents with keyword search!")
                        else:
                            st.error("❌ Failed to load documents")
                    else:
                        st.session_state.simple_documents = load_documents_simple(doc_directory)
                        st.session_state.use_simple_search = True
                        if st.session_state.simple_documents:
                            st.success(f"✅ Loaded {len(st.session_state.simple_documents)} documents with keyword search!")
                        else:
                            st.error("❌ Failed to load documents")
        
        # Model parameters
        st.subheader("🎛️ Response Settings")
        max_tokens = st.slider("Response length", 50, 1024, 512, help="Maximum length of AI responses")
        temperature = st.slider("Response creativity", 0.0, 1.0, 0.3, 0.1, help="Lower values = more focused responses")
        
        # Modal status
        st.subheader("🔗 AI Connection")
        if st.button("Test AI Connection"):
            with st.spinner("Testing connection..."):
                test_response = call_modal_llm("Hello", max_tokens=10, temperature=0.1)
                if test_response:
                    st.success("✅ AI is connected and ready")
                else:
                    st.error("❌ AI connection failed")
        
        # Quick actions
        st.subheader("🚀 Quick Actions")
        if st.button("🗑️ Clear Chat History"):
            st.session_state.messages = []
            st.rerun()
    
    # Sample questions section
    st.subheader("💡 Sample Questions")
    sample_questions = get_sample_questions()
    
    # Display sample questions in columns
    col1, col2 = st.columns(2)
    for i, question in enumerate(sample_questions):
        col = col1 if i % 2 == 0 else col2
        with col:
            if st.button(question, key=f"sample_{i}", help="Click to ask this question"):
                # Add the question to chat
                if "messages" not in st.session_state:
                    st.session_state.messages = []
                st.session_state.messages.append({"role": "user", "content": question})
                st.rerun()
    
    # Initialize chat history
    if "messages" not in st.session_state:
        st.session_state.messages = []
        # Add welcome message
        welcome_msg = """👋 Welcome! I'm here to help you provide excellent mental health customer service. 

You can ask me about:
- Communication techniques and active listening
- Handling difficult situations with empathy
- Trauma-informed care approaches
- Professional boundaries and self-care
- Crisis intervention basics

How can I assist you today?"""
        st.session_state.messages.append({"role": "assistant", "content": welcome_msg})
    
    # Display chat messages
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    
    # Chat input
    if prompt := st.chat_input("Ask about mental health customer service..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # Generate response
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                # Check if we should use document context
                if use_documents:
                    if (hasattr(st.session_state, 'index') and 
                        st.session_state.index is not None and
                        not getattr(st.session_state, 'use_simple_search', False)):
                        # Use vector search
                        response = query_with_context(
                            st.session_state.index, 
                            prompt, 
                            lambda p: call_modal_llm(p, max_tokens, temperature)
                        )
                    elif (hasattr(st.session_state, 'simple_documents') and 
                          st.session_state.simple_documents):
                        # Use simple search
                        context = simple_context_search(st.session_state.simple_documents, prompt)
                        if context:
                            enhanced_prompt = f"""You are a knowledgeable mental health customer service assistant. Use the following context to provide helpful, empathetic, and professional responses.

Context: {context}

Customer Service Question: {prompt}

Please provide a compassionate and informative response based on the context. Focus on practical guidance for customer service representatives. If the context doesn't contain relevant information, provide general mental health support guidance while being clear about your limitations."""
                            response = call_modal_llm(enhanced_prompt, max_tokens, temperature)
                        else:
                            # No context found, provide general mental health customer service response
                            general_prompt = f"""You are a mental health customer service assistant. Please provide helpful guidance for this question: {prompt}

Focus on practical, empathetic customer service techniques. Always emphasize the importance of following organizational protocols and seeking supervision when needed."""
                            response = call_modal_llm(general_prompt, max_tokens, temperature)
                    else:
                        # No documents loaded, provide general guidance
                        general_prompt = f"""You are a mental health customer service assistant. Please provide helpful guidance for this question: {prompt}

Focus on practical, empathetic customer service techniques. Always emphasize the importance of following organizational protocols and seeking supervision when needed."""
                        response = call_modal_llm(general_prompt, max_tokens, temperature)
                else:
                    response = call_modal_llm(prompt, max_tokens, temperature)
                
                if response:
                    st.markdown(response)
                    # Add assistant response to chat history
                    st.session_state.messages.append({"role": "assistant", "content": response})
                else:
                    st.error("❌ Failed to get response from the AI model")

if __name__ == "__main__":
    main() 