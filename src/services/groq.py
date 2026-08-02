import logging
import os

from dotenv import load_dotenv
from groq import Groq


load_dotenv()

client = Groq(api_key=os.getenv("GROQ_KEY"))
logger = logging.getLogger(__name__)


def groq_call(**payload):
    logger.debug("Creating Groq completion with payload keys: %s", sorted(payload.keys()))
    return client.chat.completions.create(**payload)