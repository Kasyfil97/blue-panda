#!/usr/bin/env python3
"""
OIDC Federation: Invoke Bedrock Model (gpt-oss-120b)
Flow: Entra ID -> common-security (bridge) -> target account (Bedrock)
"""

import os
import re
import json
import time
import base64
import requests
import boto3
from dotenv import load_dotenv

load_dotenv()

DEFAULT_REGION = "ap-southeast-3"
DEFAULT_MODEL_ID = "openai.gpt-oss-120b-1:0"
SESSION_REFRESH_SECONDS = 3000  # refresh creds ~50 min before 1h expiry


class BedrockSession:
    """Manages OIDC federation and AWS credential lifecycle.

    Flow: Entra ID token -> bridge role (common-security) -> target role (Bedrock).
    Call refresh_if_needed() before each request to auto-renew before expiry.
    """

    def __init__(self, region=None, model_id=None):
        self.region = region or os.getenv("AWS_REGION", DEFAULT_REGION)
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        self.session = None
        self.target_creds = None
        self._created_at = None
        self.setup()

    def setup(self):
        """Perform the full 3-step OIDC federation and build a boto3 Session."""
        print("🔐 Setting up OIDC session...")

        access_token = self._get_entra_token()
        if not access_token:
            raise RuntimeError("Failed to get Entra ID token")

        try:
            bridge_creds = self._assume_bridge_role(access_token)
        except Exception as e:
            raise RuntimeError(f"Step 2 FAILED (bridge role): {e}\n"
                               "   Hint: Pastikan OIDC Provider terdaftar di account common-security")

        try:
            self.target_creds = self._assume_target_role(bridge_creds)
        except Exception as e:
            raise RuntimeError(f"Step 3 FAILED (target role): {e}\n"
                               "   Hint: Pastikan bridge role punya permission sts:AssumeRole ke target")

        self.session = boto3.Session(
            aws_access_key_id=self.target_creds["AccessKeyId"],
            aws_secret_access_key=self.target_creds["SecretAccessKey"],
            aws_session_token=self.target_creds["SessionToken"],
            region_name=self.region,
        )
        self._created_at = time.time()

        identity = self.session.client("sts").get_caller_identity()
        # R13 log hygiene: don't print the full identity ARN at default verbosity.
        if os.getenv("T2S_DEBUG"):
            print(f"✅ Session ready — Identity: {identity['Arn']}")
        else:
            print("✅ Session ready")

    def refresh_if_needed(self):
        """Re-run setup() if credentials are close to expiry."""
        age = time.time() - self._created_at
        if age >= SESSION_REFRESH_SECONDS:
            print(f"\n🔄 Credentials age {int(age)}s — refreshing session...")
            self.setup()

    def invoke(self, messages, tools=None, max_tokens=2048, temperature=0.0,
               *, tool_choice=None, allow_mantle=False):
        """Invoke the model with an OpenAI-style body and return the raw assistant
        ``message`` dict (``choices[0].message``), including ``tool_calls`` when present.

        This is the seam the Strands custom model provider depends on (plan KD2). Unlike
        the module-level ``_invoke_standard`` it: carries a ``tools`` array, uses
        caller-supplied ``max_tokens``/``temperature`` (not 512/0.7), returns the whole
        message (not just ``content``), and **raises** on failure instead of returning
        ``None`` / silently falling back to Mantle. The ``bedrock-runtime`` client is
        rebuilt from ``self.session`` on every call (after ``refresh_if_needed``), so a
        refreshed session's new credentials are always used.
        """
        self.refresh_if_needed()
        body_dict = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body_dict["tools"] = tools
        if tool_choice is not None:
            body_dict["tool_choice"] = tool_choice
        body = json.dumps(body_dict)

        try:
            return self._invoke_once(body)
        except Exception as exc:  # noqa: BLE001
            if _is_expired_credentials(exc):
                # One forced re-auth + retry on mid-session token expiry.
                print("🔄 Credentials expired mid-call — re-authenticating and retrying...")
                self.setup()
                return self._invoke_once(body)
            if allow_mantle:
                print(f"⚡ invoke() standard path failed ({type(exc).__name__}); "
                      "trying Mantle fallback...")
                return self._invoke_mantle_message(body)
            raise

    def _invoke_once(self, body):
        """Single invoke_model round-trip → raw message dict, or raise."""
        runtime = self.session.client("bedrock-runtime", region_name=self.region)
        resp = runtime.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        rb = json.loads(resp["body"].read())
        if "choices" in rb and rb["choices"]:
            return rb["choices"][0]["message"]
        raise RuntimeError(f"Unexpected invoke_model response shape: {json.dumps(rb)[:300]}")

    def _invoke_mantle_message(self, body):
        """Degraded Mantle (OpenAI-compatible) fallback → raw message dict, or raise.

        Off by default (``allow_mantle=False``); uses a bearer/SigV4-signed OpenAI
        endpoint, a different auth path than the federated session.
        """
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        creds = self.target_creds
        mantle_url = f"https://bedrock-mantle.{self.region}.api.aws/v1/chat/completions"
        # Mantle wants the model id inside the body too.
        payload = json.loads(body)
        payload["model"] = self.model_id
        data = json.dumps(payload)

        aws_request = AWSRequest(
            method="POST", url=mantle_url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        SigV4Auth(
            Credentials(creds["AccessKeyId"], creds["SecretAccessKey"],
                        creds["SessionToken"]),
            "bedrock", self.region,
        ).add_auth(aws_request)
        resp = requests.post(mantle_url, headers=dict(aws_request.headers),
                             data=data, timeout=60)
        resp.raise_for_status()
        rb = resp.json()
        if "choices" in rb and rb["choices"]:
            return rb["choices"][0]["message"]
        raise RuntimeError(f"Unexpected Mantle response shape: {json.dumps(rb)[:300]}")

    def list_models(self):
        """Print available Bedrock foundation models."""
        bedrock = self.session.client("bedrock", region_name=self.region)
        print(f"\n📋 Bedrock models in {self.region}:")
        try:
            models = bedrock.list_foundation_models().get("modelSummaries", [])
            for m in models[:10]:
                print(f"   - {m['modelId']} ({m.get('providerName', 'N/A')})")
            if len(models) > 10:
                print(f"   ... dan {len(models) - 10} model lainnya")
        except Exception as e:
            print(f"❌ Error listing models: {e}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_entra_token(self):
        token_url = (
            f"https://login.microsoftonline.com/"
            f"{os.getenv('AZURE_TENANT_ID')}/oauth2/v2.0/token"
        )
        resp = requests.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("AZURE_CLIENT_ID"),
            "client_secret": os.getenv("AZURE_CLIENT_SECRET"),
            "scope": f"{os.getenv('AZURE_CLIENT_ID')}/.default",
        })
        if resp.status_code != 200:
            print(f"❌ Entra token FAILED: {resp.json().get('error_description')}")
            return None

        access_token = resp.json()["access_token"]
        parts = access_token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
        print(f"✅ Entra token OK  iss={payload.get('iss')}  aud={payload.get('aud')}")
        return access_token

    def _assume_bridge_role(self, access_token):
        sts = boto3.client("sts", region_name=self.region)
        resp = sts.assume_role_with_web_identity(
            RoleArn=os.getenv("AWS_ROLE_ARN_BRIDGE"),
            RoleSessionName="oidc-bridge-bedrock",
            WebIdentityToken=access_token,
            DurationSeconds=3600,
        )
        print("✅ Bridge role assumed (common-security)")
        return resp["Credentials"]

    def _assume_target_role(self, bridge_creds):
        sts = boto3.client(
            "sts",
            aws_access_key_id=bridge_creds["AccessKeyId"],
            aws_secret_access_key=bridge_creds["SecretAccessKey"],
            aws_session_token=bridge_creds["SessionToken"],
            region_name=self.region,
        )
        resp = sts.assume_role(
            RoleArn=os.getenv("AWS_ROLE_ARN_TARGET"),
            RoleSessionName="bedrock-target-session",
            DurationSeconds=3600,
        )
        print("✅ Target role assumed")
        return resp["Credentials"]


def chat(bedrock_session: BedrockSession):
    """Run a continuous multi-turn conversation against the Bedrock model.

    Maintains message history for context. Session credentials are refreshed
    automatically every SESSION_REFRESH_SECONDS to prevent expiry mid-chat.
    Type 'exit' or 'quit' to end the loop.
    """
    region = bedrock_session.region
    model_id = bedrock_session.model_id
    history = []

    print("\n" + "=" * 60)
    print(f"💬 Chat started  model={model_id}  region={region}")
    print("   Type 'exit' or 'quit' to end.")
    print("=" * 60 + "\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Bye!")
            break

        # Refresh credentials before each request if needed
        bedrock_session.refresh_if_needed()

        history.append({"role": "user", "content": user_input})

        response_text = _invoke(bedrock_session, history)
        if response_text is None:
            # _invoke already printed the error; pop the failed turn so history stays clean
            history.pop()
            continue

        history.append({"role": "assistant", "content": response_text})
        print(f"\nAssistant: {response_text}\n")


# ------------------------------------------------------------------
# Internal invoke helpers (standard bedrock-runtime → Mantle fallback)
# ------------------------------------------------------------------

def _is_expired_credentials(exc: Exception) -> bool:
    """True if the exception looks like an expired/invalid AWS security token."""
    text = f"{type(exc).__name__}: {exc}"
    markers = ("ExpiredToken", "ExpiredTokenException", "InvalidSignatureException",
               "security token included in the request is expired",
               "The provided token has expired", "UnrecognizedClientException")
    return any(m.lower() in text.lower() for m in markers)


def _strip_reasoning(text: str) -> str:
    """Remove any <reasoning>...</reasoning> block, returning only the answer.

    gpt-oss models may prepend their chain-of-thought wrapped in <reasoning>
    tags. We only want the final response shown to the user.
    """
    if not text:
        return text
    cleaned = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def _invoke(bedrock_session: BedrockSession, messages: list) -> str | None:
    """Send messages to Bedrock, falling back to Mantle on failure.
    Returns the assistant text, or None on unrecoverable error.
    """
    result = _invoke_standard(bedrock_session, messages)
    if result is None:
        print("⚡ Falling back to Bedrock Mantle (OpenAI-compatible) endpoint...")
        result = _invoke_mantle(bedrock_session, messages)

    if result is not None:
        result = _strip_reasoning(result)
    return result


def _invoke_standard(bedrock_session: BedrockSession, messages: list) -> str | None:
    runtime = bedrock_session.session.client(
        "bedrock-runtime", region_name=bedrock_session.region
    )
    body = json.dumps({"messages": messages, "max_tokens": 512, "temperature": 0.7})
    try:
        resp = runtime.invoke_model(
            modelId=bedrock_session.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        rb = json.loads(resp["body"].read())
        if "choices" in rb:
            return rb["choices"][0]["message"]["content"]
        if "content" in rb:
            return rb["content"][0]["text"]
        if "output" in rb:
            return rb["output"]
        return json.dumps(rb)[:300]

    except runtime.exceptions.AccessDeniedException as e:
        print(f"❌ AccessDenied: {e}")
    except runtime.exceptions.ValidationException as e:
        print(f"❌ ValidationError: {e}")
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
    return None


def _invoke_mantle(bedrock_session: BedrockSession, messages: list) -> str | None:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    region = bedrock_session.region
    creds = bedrock_session.target_creds
    mantle_url = f"https://bedrock-mantle.{region}.api.aws/v1/chat/completions"

    body = json.dumps({
        "model": bedrock_session.model_id,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.7,
    })
    aws_request = AWSRequest(
        method="POST",
        url=mantle_url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    SigV4Auth(
        Credentials(creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]),
        "bedrock",
        region,
    ).add_auth(aws_request)

    try:
        resp = requests.post(
            mantle_url, headers=dict(aws_request.headers), data=body, timeout=60
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        print(f"❌ [Mantle] HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        print(f"❌ [Mantle] {type(e).__name__}: {e}")
    return None


def main():
    print("=" * 60)
    print("🔐 Bedrock Chat via OIDC Federation (No API Key)")
    print(f"   Model:  {os.getenv('BEDROCK_MODEL_ID', DEFAULT_MODEL_ID)}")
    print(f"   Region: {os.getenv('AWS_REGION', DEFAULT_REGION)}")
    print(f"   Bridge: {os.getenv('AWS_ROLE_ARN_BRIDGE')}")
    print(f"   Target: {os.getenv('AWS_ROLE_ARN_TARGET')}")
    print("=" * 60 + "\n")

    try:
        session = BedrockSession()
    except RuntimeError as e:
        print(f"❌ Session setup failed: {e}")
        return

    session.list_models()
    chat(session)


if __name__ == "__main__":
    main()