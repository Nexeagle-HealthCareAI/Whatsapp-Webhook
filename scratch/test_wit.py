import asyncio
import os
import sys

# Setup paths to import from app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set fallback envs for loading Settings
os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")

# Import config and NLU client
from app.config import settings
from app.wit_client import parse_message_intent

async def run_diagnostics():
    print("=" * 60)
    print("WIT.AI DIAGNOSTIC TEST RUN")
    print("=" * 60)
    
    # Print configuration details
    token = settings.wit_server_token
    print(f"Loaded Settings Server Token: {token}")
    if not token or token == "test":
        print("[WARNING]: No real WIT_SERVER_TOKEN set! Please check your environment variables.")
        # Try reading directly from .env file if it exists
        dot_env_path = os.path.join(os.path.dirname(__file__), '../.env')
        if os.path.exists(dot_env_path):
            print(f".env file found at {dot_env_path}. Reading directly...")
            with open(dot_env_path) as f:
                for line in f:
                    if line.startswith("WIT_SERVER_TOKEN="):
                        val = line.strip().split("=", 1)[1]
                        print(f"Direct token found in .env: {val}")
                        settings.wit_server_token = val
                        token = val
    
    sample_text = "sorry i change my mind, please change my doctor to Dr Avinash"
    print(f"\nSending test query: \"{sample_text}\"")
    
    try:
        result = await parse_message_intent(sample_text)
        print("\n=== Parsed NLU Result ===")
        print(f"Intent:      {result.get('intent')} (confidence: {result.get('confidence')})")
        print(f"Doctor:      {result.get('doctor_name')}")
        print(f"Specialty:   {result.get('specialty')}")
        print(f"Symptom:     {result.get('symptom')}")
        print(f"Date:        {result.get('formatted_date')}")
        print("=========================")
        print("\n[SUCCESS]: API response successfully received and parsed!")
    except Exception as exc:
        print(f"\n[ERROR]: Wit.ai API call failed: {exc}")
        
if __name__ == "__main__":
    asyncio.run(run_diagnostics())
