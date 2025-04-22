from typing import List

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer

app = FastAPI(title="LLM Chat API")

# Model and tokenizer will be loaded at startup
model = None
tokenizer = None

class ChatRequest(BaseModel):
    messages: List[str]
    max_length: int = 100
    temperature: float = 0.7

class ChatResponse(BaseModel):
    response: str

@app.on_event("startup")
async def startup_event():
    global model, tokenizer
    model_path = "./output"  # Path to your trained model
    model = AutoModelForCausalLM.from_pretrained(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    if torch.cuda.is_available():
        model = model.cuda()

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        # Prepare input
        input_text = " ".join(request.messages)
        inputs = tokenizer(input_text, return_tensors="pt")
        
        if torch.cuda.is_available():
            inputs = {k: v.cuda() for k, v in inputs.items()}

        # Generate response
        outputs = model.generate(
            **inputs,
            max_length=request.max_length,
            temperature=request.temperature,
            do_sample=True,
        )

        # Decode and return response
        response = tokenizer.decode(outputs[0], skip_special_tokens=True)
        return ChatResponse(response=response)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 