# To run this app, use the command: streamlit run py-notebook/site/flask/chat_app.py
import streamlit as st
import openai
import os

# Set your OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

st.title("Financial Advisor Chatbot")

# Initialize session state for chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display chat messages from history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Accept user input
if prompt := st.chat_input("Ask your financial question here..."):
    # Add a system prompt to guide the assistant as a financial advisor
    if not any(msg["role"] == "system" for msg in st.session_state.messages):
        system_prompt = {
            "role": "system",
            "content": (
                "You are a helpful and knowledgeable financial advisor. "
                "Provide clear, accurate, and actionable financial advice. "
                "If you are unsure or the question is outside your expertise, "
                "politely let the user know."
            )
        }
        st.session_state.messages.insert(0, system_prompt)

    # Display user message in chat message container
    st.chat_message("user").markdown(prompt)
    # Append user message to chat history
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Generate assistant response
    response = openai.ChatCompletion.create(
        model="gpt-4.1",
        messages=st.session_state.messages
    )
    assistant_reply = response.choices[0].message["content"]

    # Display assistant response in chat message container
    st.chat_message("assistant").markdown(assistant_reply)
    # Append assistant response to chat history
    st.session_state.messages.append({"role": "assistant", "content": assistant_reply})
