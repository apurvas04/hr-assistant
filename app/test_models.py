import os, time
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

names = [m.name.replace("models/", "") for m in client.models.list()
         if "generateContent" in (m.supported_actions or []) and "flash" in m.name]

for name in names:
    t = time.time()
    try:
        r = client.models.generate_content(model=name, contents="Reply with the word OK")
        print(f"WORKS  {name:45} {time.time()-t:.1f}s  -> {r.text.strip()[:20]}")
    except Exception as e:
        print(f"FAILS  {name:45} {str(e)[:70]}")