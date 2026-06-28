import os
import requests
from dotenv import load_dotenv

load_dotenv()

def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ Error: GEMINI_API_KEY not found in .env.")
        return
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    print(f"Making direct REST request to: https://generativelanguage.googleapis.com/v1beta/models?key={api_key[:6]}...")
    
    try:
        response = requests.get(url)
        print(f"Response Status Code: {response.status_code}")
        
        if response.status_code == 200:
            print("\n✅ Success! Available models for your key and region:")
            data = response.json()
            for model in data.get('models', []):
                print(f"- {model['name']} (displayName: {model['displayName']})")
        else:
            print(f"\n❌ API returned error code {response.status_code}:")
            print(response.text)
            
    except Exception as e:
        print(f"❌ Connection error: {e}")

if __name__ == "__main__":
    main()
