# To run this app, use the command: streamlit run web/chat_app.py
import streamlit as st
import openai
import os
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader

# Set your OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Build or load the FAISS index
if "index" not in st.session_state:
    documents = SimpleDirectoryReader('docs').load_data()
    st.session_state.index = VectorStoreIndex.from_documents(documents)

st.title("Financial Advisor Chatbot with RAG")

# Initialize session state for chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display chat messages from history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Accept user input
if prompt := st.chat_input("Ask your financial question here..."):
    # RAG: Retrieve context using FAISS
    query_engine = st.session_state.index.as_query_engine()
    rag_response = query_engine.query(prompt)
    context = str(rag_response)

    # Add system prompt and context
    if not any(msg["role"] == "system" for msg in st.session_state.messages):
        st.session_state.messages.insert(0, {
            "role": "system",
            "content": (
                "You are a helpful and knowledgeable financial advisor. "
                "Provide clear, accurate, and actionable financial advice. "
                "If you are unsure or the question is outside your expertise, "
                "politely let the user know."
            )
        })
    # Insert context as a system message
    st.session_state.messages.insert(1, {
        "role": "system",
        "content": f"Use the following context to answer the user's question:\n{context}"
    })

    # Display user message in chat message container
    st.chat_message("user").markdown(prompt)
    # Append user message to chat history
    st.session_state.messages.append({"role": "user", "content": prompt})

    # OpenAI call (use updated API as previously discussed)
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=st.session_state.messages
    )
    assistant_reply = response.choices[0].message.content

    # Display assistant response in chat message container
    st.chat_message("assistant").markdown(assistant_reply)
    # Append assistant response to chat history
    st.session_state.messages.append({"role": "assistant", "content": assistant_reply})
