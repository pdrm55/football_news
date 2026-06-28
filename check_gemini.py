import os
import sys
from dotenv import load_dotenv
from google import genai

load_dotenv()

def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ Error: GEMINI_API_KEY is not defined in your .env file.")
        return
        
    print(f"Checking Gemini API connectivity with Key: {api_key[:6]}...{api_key[-4:] if len(api_key) > 10 else ''}")
    
    # Initialize the new SDK client
    client = genai.Client(api_key=api_key)
    
    test_models = [
        'gemini-1.5-flash',
        'models/gemini-1.5-flash',
        'gemini-1.5-pro',
        'models/gemini-1.5-pro',
        'gemini-1.0-pro',
        'models/gemini-1.0-pro'
    ]
    
    print("\nTesting multiple model names:")
    print("-" * 60)
    for model_name in test_models:
        try:
            print(f"Testing model '{model_name}'...")
            response = client.models.generate_content(
                model=model_name,
                contents="Hello"
            )
            print(f"  ✅ SUCCESS! Response: {response.text.strip()}")
            print("-" * 60)
            break
        except Exception as e:
            print(f"  ❌ FAILED: {e}")
            print("-" * 60)

if __name__ == "__main__":
    main()
