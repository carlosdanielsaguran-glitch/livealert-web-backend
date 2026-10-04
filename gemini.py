"""
LiveAlert — Gemini AI
======================
Generates incident descriptions and severity levels
using Google Gemini API.

Install:
    pip install google-genai
"""
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()


client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

MODEL_NAME = "gemini-2.0-flash"


def generate_incident_summary(incident_type: str, location: str = "") -> dict:
    """
    Generates an AI summary and severity level for an incident.
    Returns: { "description": str, "level": int }
    """
    prompt = f"""
You are an emergency dispatch assistant for LiveAlert.
Given an incident type, generate:
1. A 2-3 sentence professional incident summary for responders
2. A severity level from 1 to 3 (1=minor, 2=moderate, 3=critical)

Incident Type: {incident_type}
Location: {location or "Unknown"}

Respond in this exact format:
DESCRIPTION: <your 2-3 sentence summary>
LEVEL: <1, 2, or 3>
"""

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        text = response.text.strip()

        description = ""
        level = 1

        for line in text.splitlines():
            if line.startswith("DESCRIPTION:"):
                description = line.replace("DESCRIPTION:", "").strip()
            elif line.startswith("LEVEL:"):
                try:
                    level = int(line.replace("LEVEL:", "").strip())
                except ValueError:
                    level = 1

        return {"description": description, "level": level}

    except Exception as e:
        return {
            "description": f"Emergency reported: {incident_type} at {location or 'unknown location'}.",
            "level": 1,
            "error": str(e),
        }