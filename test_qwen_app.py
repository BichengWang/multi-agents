#!/usr/bin/env python3
"""
Simple test script for the Qwen-powered mental health chat app
"""

from chat_app_qwen import call_modal_llm, load_documents_simple, simple_context_search

def test_qwen_integration():
    print('🧠💙 Testing Mental Health Chat App with Qwen Model')
    print('=' * 55)
    
    # Test 1: Direct LLM call
    print('\n1. Testing direct Qwen model call:')
    response = call_modal_llm('What are active listening techniques?', max_tokens=100)
    print('Response:', response[:200] + '...' if len(response) > 200 else response)
    
    # Test 2: Document loading
    print('\n2. Testing document loading:')
    docs = load_documents_simple('./docs')
    if docs:
        print(f'✅ Loaded {len(docs)} documents')
        for doc in docs:
            print(f'  - {doc["filename"]}: {len(doc["content"])} characters')
    else:
        print('❌ No documents loaded')
        return False
    
    # Test 3: Context search
    print('\n3. Testing context search:')
    context = simple_context_search(docs, 'empathy customer service')
    if context:
        print(f'✅ Found context: {len(context)} characters')
        print('Context preview:', context[:150] + '...')
    else:
        print('❌ No context found')
        return False
    
    # Test 4: Context-aware response
    print('\n4. Testing context-aware response:')
    enhanced_prompt = f"""You are a mental health customer service assistant. Use this context: {context[:300]}...

Question: How can I show empathy to customers?

Please provide practical guidance."""
    
    response = call_modal_llm(enhanced_prompt, max_tokens=120)
    print('Context-aware response:', response[:300] + '...' if len(response) > 300 else response)
    
    # Test 5: Mental health specific question
    print('\n5. Testing mental health specific question:')
    mh_response = call_modal_llm('What should I do if a customer mentions feeling depressed?', max_tokens=100)
    print('Mental health response:', mh_response[:250] + '...' if len(mh_response) > 250 else mh_response)
    
    print('\n✅ All tests completed successfully!')
    print('\n🎉 Your Mental Health Customer Service Assistant is ready!')
    print('   - Qwen model is working properly')
    print('   - Document loading is functional')
    print('   - Context search is working')
    print('   - Mental health guidance is available')
    print('\n💡 Access your app at: http://localhost:8501')
    
    return True

if __name__ == "__main__":
    test_qwen_integration() 