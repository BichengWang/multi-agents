#!/usr/bin/env python3
"""
Test script for the Qwen chat application
Tests document loading and indexing functionality with mental health documentation
"""

import os
import sys
from llama_index.core import SimpleDirectoryReader, VectorStoreIndex
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

def test_document_loading():
    """Test document loading functionality from docs directory"""
    print("Testing document loading from docs directory...")
    
    # Test with docs directory
    if not os.path.exists("./docs"):
        print("❌ Docs directory not found")
        return False
    
    try:
        documents = SimpleDirectoryReader("./docs").load_data()
        
        if not documents:
            print("❌ No documents loaded from docs directory")
            return False
        
        print(f"✅ Successfully loaded {len(documents)} documents from docs")
        
        # Print document info
        for i, doc in enumerate(documents):
            print(f"  Document {i+1}: {len(doc.text)} characters")
            # Get filename from metadata if available
            filename = getattr(doc, 'metadata', {}).get('file_name', f'Document {i+1}')
            print(f"    File: {filename}")
            print(f"    Preview: {doc.text[:100]}...")
        
        return True
    except Exception as e:
        print(f"❌ Error loading documents: {e}")
        return False

def test_mental_health_content():
    """Test that mental health documentation is properly loaded"""
    print("\nTesting mental health documentation content...")
    
    try:
        documents = SimpleDirectoryReader("./docs").load_data()
        if not documents:
            print("❌ Cannot test mental health content - no documents loaded")
            return False
        
        # Check for mental health related content
        mental_health_keywords = [
            "mental health", "psychology", "anxiety", "depression", 
            "trauma", "therapy", "counseling", "empathy", "active listening"
        ]
        
        found_keywords = set()
        for doc in documents:
            text_lower = doc.text.lower()
            for keyword in mental_health_keywords:
                if keyword in text_lower:
                    found_keywords.add(keyword)
        
        if len(found_keywords) >= 5:  # Expect at least 5 mental health keywords
            print(f"✅ Mental health content verified - found keywords: {', '.join(sorted(found_keywords))}")
            return True
        else:
            print(f"❌ Insufficient mental health content - only found: {', '.join(sorted(found_keywords))}")
            return False
            
    except Exception as e:
        print(f"❌ Error testing mental health content: {e}")
        return False

def test_index_creation():
    """Test index creation functionality with mental health docs"""
    print("\nTesting index creation with mental health documentation...")
    
    # Load documents first
    try:
        documents = SimpleDirectoryReader("./docs").load_data()
        if not documents:
            print("❌ Cannot test index creation - no documents loaded")
            return False
        
        print(f"  Loaded {len(documents)} documents for indexing")
        
        # Create index with default embedding model
        print("  Creating vector index with default embedding...")
        index = VectorStoreIndex.from_documents(documents)
        print("  ✅ Vector index created")
        
        print("✅ Successfully created vector index")
        
        # Test mental health related queries
        print("  Testing mental health query engine...")
        query_engine = index.as_query_engine()
        
        test_queries = [
            "How should I handle anxiety in customer service?",
            "What are active listening techniques?",
            "How do I show empathy to customers?",
            "What is trauma-informed care?"
        ]
        
        for query in test_queries:
            try:
                response = query_engine.query(query)
                print(f"  Query: {query}")
                print(f"  Response preview: {str(response)[:150]}...")
                print()
            except Exception as e:
                print(f"  ❌ Error with query '{query}': {e}")
        
        return True
    except Exception as e:
        import traceback
        print(f"❌ Error creating index: {e}")
        print(f"  Full traceback: {traceback.format_exc()}")
        return False

def test_modal_config():
    """Test modal configuration loading"""
    print("\nTesting modal configuration...")
    
    try:
        from modal_config import MODAL_CONFIG, is_mock_enabled
        
        if not MODAL_CONFIG:
            print("❌ Modal config not found")
            return False
        
        if "app_url" not in MODAL_CONFIG:
            print("❌ Modal config missing app_url")
            return False
        
        print(f"✅ Modal config loaded: {MODAL_CONFIG['app_url']}")
        print(f"  Mock mode enabled: {is_mock_enabled()}")
        print(f"  Timeout: {MODAL_CONFIG.get('timeout', 'default')}")
        print(f"  Max retries: {MODAL_CONFIG.get('max_retries', 'default')}")
        return True
    except Exception as e:
        print(f"❌ Error loading modal config: {e}")
        return False

def test_chat_app_imports():
    """Test that the chat app can be imported successfully"""
    print("\nTesting chat app imports...")
    
    try:
        from chat_app_qwen import load_documents_simple, simple_context_search
        print("✅ Successfully imported chat app functions")
        
        # Test document loading function
        docs = load_documents_simple('./docs')
        if docs:
            print(f"✅ Document loading function works - loaded {len(docs)} documents")
        else:
            print("⚠️  Document loading function returned no documents")
        
        # Test context search
        if docs:
            context = simple_context_search(docs, "mental health")
            print(f"✅ Context search function works - found {len(context)} characters of context")
        
        return True
    except Exception as e:
        print(f"❌ Error importing chat app: {e}")
        return False

def setup_test_environment():
    """Set up test environment with proper directory structure"""
    print("Setting up test environment...")
    
    # Ensure docs directory exists
    if not os.path.exists("./docs"):
        print("❌ Docs directory not found. Please ensure mental health documentation is in ./docs/")
        return False
    
    # Check for required files
    required_files = [
        "./docs/mental_health_customer_service_manual.txt",
        "./docs/psychology_fundamentals.txt"
    ]
    
    missing_files = []
    for file_path in required_files:
        if not os.path.exists(file_path):
            missing_files.append(file_path)
    
    if missing_files:
        print(f"❌ Missing required files: {', '.join(missing_files)}")
        return False
    
    print("✅ Test environment setup complete")
    return True

def main():
    """Run all tests"""
    print("🧪 Testing Qwen Chat Application with Mental Health Documentation\n")
    
    # Setup test environment
    if not setup_test_environment():
        print("❌ Test environment setup failed")
        return 1
    
    # Run tests
    tests = [
        test_modal_config,
        test_chat_app_imports,
        test_document_loading,
        test_mental_health_content,
        test_index_creation
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"❌ Test failed with exception: {e}")
    
    print(f"\n📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! The mental health chat application should work correctly.")
        print("\n💡 Next steps:")
        print("  1. Run 'streamlit run chat_app_qwen.py' to start the application")
        print("  2. Test the mental health knowledge by asking questions about:")
        print("     - Active listening techniques")
        print("     - Handling anxiety in customer service")
        print("     - Trauma-informed care principles")
        print("     - Empathy and communication skills")
        return 0
    else:
        print("⚠️  Some tests failed. Check the errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main()) 