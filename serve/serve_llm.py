import modal
from fastapi import FastAPI
from transformers import pipeline, AutoTokenizer, AutoModelForCausalLM
import torch

app = modal.App("qwen-chat-llm")

image = modal.Image.debian_slim().pip_install(
    "transformers", "torch", "accelerate", "fastapi", "bitsandbytes"
)

# Create FastAPI app
web_app = FastAPI()

# Global variables to store the model and tokenizer
model_pipeline = None
tokenizer = None
model = None

@app.function(
    image=image, 
    gpu="A10G", 
    timeout=600,
    container_idle_timeout=300,
    allow_concurrent_inputs=10
)
@modal.web_endpoint(method="POST", label="qwen-chat")
def generate_text(request: dict):
    """Generate text using the Qwen LLM"""
    global model_pipeline, tokenizer, model
    
    # Load model if not already loaded
    if model_pipeline is None:
        print("Loading Qwen model...")
        # Use Qwen2.5-1.5B-Instruct - a proper Qwen chat model
        model_id = "Qwen/Qwen2.5-1.5B-Instruct"
        
        try:
            # Load tokenizer and model separately for better control
            tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                model_id, 
                torch_dtype=torch.float16,
                device_map="auto",
                trust_remote_code=True
            )
            
            # Create pipeline
            model_pipeline = pipeline(
                "text-generation", 
                model=model, 
                tokenizer=tokenizer,
                torch_dtype=torch.float16,
                device_map="auto"
            )
            print(f"Qwen model {model_id} loaded successfully!")
        except Exception as e:
            print(f"Error loading Qwen model: {e}")
            # Fallback to a simpler model if Qwen fails
            print("Falling back to microsoft/DialoGPT-medium...")
            model_id = "microsoft/DialoGPT-medium"
            model_pipeline = pipeline("text-generation", model=model_id, device=0)
            print(f"Fallback model {model_id} loaded successfully!")
    
    try:
        prompt = request.get("prompt", "")
        max_tokens = request.get("max_tokens", 100)
        temperature = request.get("temperature", 0.7)
        
        if not prompt:
            return {"error": "No prompt provided"}
        
        print(f"Generating response for prompt: {prompt[:50]}...")
        
        # Format prompt for Qwen chat model using the proper chat template
        if "Qwen" in str(model_pipeline.model.config._name_or_path):
            # Use Qwen's proper chat format
            messages = [{"role": "user", "content": prompt}]
            chat_prompt = tokenizer.apply_chat_template(
                messages, 
                tokenize=False, 
                add_generation_prompt=True
            )
        else:
            # Use regular format for fallback models
            chat_prompt = prompt
        
        # Generate response
        result = model_pipeline(
            chat_prompt, 
            max_new_tokens=max_tokens, 
            do_sample=True, 
            temperature=temperature,
            pad_token_id=tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            repetition_penalty=1.1
        )
        
        response = result[0]["generated_text"]
        
        # Clean up the response
        if "Qwen" in str(model_pipeline.model.config._name_or_path):
            # Remove the chat template and get only the assistant's response
            if chat_prompt in response:
                response = response[len(chat_prompt):].strip()
            # Remove any remaining special tokens
            response = response.replace("<|im_end|>", "").strip()
        else:
            # Remove the original prompt from the response for other models
            if response.startswith(chat_prompt):
                response = response[len(chat_prompt):].strip()
        
        response = response.strip()
        
        print("Response generated successfully!")
        return {"response": response}
        
    except Exception as e:
        print(f"Error during generation: {e}")
        return {"error": str(e)}

@app.function(image=image)
@modal.web_endpoint(method="GET", label="health-check")
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "message": "Qwen Chat LLM is running!"}

if __name__ == "__main__":
    # Deploy the app
    print("Deploying Qwen Chat LLM to Modal...")
    print("After deployment, you'll get a URL like: https://your-username--qwen-chat-llm-generate-text.modal.run")
    print("Use that URL in your modal_config.py file")