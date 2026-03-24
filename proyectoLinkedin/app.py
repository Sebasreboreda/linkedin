import os
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify
import requests
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

app = Flask(__name__)
if load_dotenv:
    load_dotenv()

# 🔑 Credenciales desde variables de entorno
REDIRECT_URI = (os.getenv("LINKEDIN_REDIRECT_URI") or "http://localhost:8000/callback").strip()
CLIENT_ID = (os.getenv("LINKEDIN_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("LINKEDIN_CLIENT_SECRET") or "").strip()
SCOPE = "openid profile email w_member_social"
LAST_ACCESS_TOKEN = None

def _validate_oauth_config():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        return (
            "<h2>❌ Falta configuración OAuth</h2>"
            "<p>Define <code>LINKEDIN_CLIENT_ID</code> y "
            "<code>LINKEDIN_CLIENT_SECRET</code> y "
            "<code>LINKEDIN_REDIRECT_URI</code> como variables de entorno.</p>"
        )
    return None

def _get_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
    }

def _get_member_urn(access_token):
    # Prefer userinfo for OpenID scopes.
    userinfo_response = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers=_get_headers(access_token),
        timeout=20
    )
    if userinfo_response.ok:
        subject = (userinfo_response.json().get("sub") or "").strip()
        if subject:
            if subject.startswith("urn:li:person:"):
                return subject, userinfo_response
            return f"urn:li:person:{subject}", userinfo_response

    # Fallback to /v2/me for legacy profile scopes.
    me_response = requests.get(
        "https://api.linkedin.com/v2/me",
        headers=_get_headers(access_token),
        timeout=20
    )
    if not me_response.ok:
        return None, me_response

    member_id = (me_response.json().get("id") or "").strip()
    if not member_id:
        return None, me_response
    return f"urn:li:person:{member_id}", me_response

@app.route("/")
def home():
    return """
    <h2>LinkedIn OAuth Demo</h2>
    <p>Inicia sesión para obtener el token y consultar datos.</p>
    <a href="/login">Ir a Login con LinkedIn</a>
    """

@app.route("/login")
def login():
    config_error = _validate_oauth_config()
    if config_error:
        return config_error

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }
    auth_url = "https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params)
    return redirect(auth_url)

@app.route("/callback")
def callback():
    global LAST_ACCESS_TOKEN
    config_error = _validate_oauth_config()
    if config_error:
        return config_error

    # 📥 Capturamos el code de la URL
    code = request.args.get("code")

    if not code:
        return "❌ No se recibió el code"

    print(f"👉 CODE recibido: {code}")  # También lo verás en consola

    # 🔄 Intercambiar code por access_token
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }

    token_response = requests.post(token_url, data=data)
    token_json = token_response.json()

    access_token = token_json.get("access_token")

    if not access_token:
        return f"""
        <h2>❌ Error obteniendo token</h2>
        <pre>{token_json}</pre>
        """

    LAST_ACCESS_TOKEN = access_token
    print(f"🔑 ACCESS TOKEN: {access_token}")

    # 📡 Llamada a la API de LinkedIn
    api_url = "https://api.linkedin.com/v2/userinfo"

    headers = _get_headers(access_token)

    api_response = requests.get(api_url, headers=headers)
    user_data = api_response.json()

    print("📦 Datos usuario:", user_data)

    # 🖥️ Mostrar todo en el navegador
    return f"""
    <h2>✅ CODE:</h2>
    <pre>{code}</pre>

    <h2>🔑 ACCESS TOKEN:</h2>
    <pre>{access_token}</pre>

    <h2>📦 DATOS USUARIO:</h2>
    <pre>{user_data}</pre>

    <h2>🔌 Share on LinkedIn endpoints:</h2>
    <ul>
      <li><a href="/me">/me</a> (perfil)</li>
      <li><a href="/posts">/posts</a> (publicaciones UGC del usuario)</li>
      <li>POST /share/text (crear publicación de texto)</li>
    </ul>
    """

@app.route("/me")
def me():
    access_token = request.args.get("access_token") or LAST_ACCESS_TOKEN
    if not access_token:
        return jsonify({"error": "No access token. Haz login en /login"}), 400

    response = requests.get("https://api.linkedin.com/v2/userinfo", headers=_get_headers(access_token), timeout=20)
    if response.status_code == 403:
        # Fallback for apps using legacy profile scopes.
        response = requests.get("https://api.linkedin.com/v2/me", headers=_get_headers(access_token), timeout=20)
    return jsonify({
        "status_code": response.status_code,
        "data": response.json()
    }), response.status_code

@app.route("/posts")
def posts():
    access_token = request.args.get("access_token") or LAST_ACCESS_TOKEN
    if not access_token:
        return jsonify({"error": "No access token. Haz login en /login"}), 400

    author_urn, me_response = _get_member_urn(access_token)
    if not author_urn:
        return jsonify({
            "error": "No se pudo obtener el URN del autor desde userinfo ni /v2/me",
            "status_code": me_response.status_code,
            "data": me_response.json()
        }), me_response.status_code

    response = requests.get(
        "https://api.linkedin.com/v2/ugcPosts",
        headers=_get_headers(access_token),
        params={"q": "authors", "authors": f"List({author_urn})"},
        timeout=20
    )
    if response.status_code == 403:
        return jsonify({
            "author": author_urn,
            "status_code": response.status_code,
            "data": response.json(),
            "hint": (
                "Tu app/token no tiene permiso para leer ugcPosts (finder authors). "
                "Share on LinkedIn suele permitir publicar (w_member_social), "
                "pero para lectura necesitas permisos/producto adicionales aprobados."
            )
        }), response.status_code
    return jsonify({
        "author": author_urn,
        "status_code": response.status_code,
        "data": response.json()
    }), response.status_code

@app.route("/share/text", methods=["POST"])
def share_text():
    access_token = request.args.get("access_token") or LAST_ACCESS_TOKEN
    if not access_token:
        return jsonify({"error": "No access token. Haz login en /login"}), 400

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({
            "error": "Falta 'text' en JSON body",
            "example": {"text": "Hola LinkedIn desde API"}
        }), 400

    author_urn, me_response = _get_member_urn(access_token)
    if not author_urn:
        return jsonify({
            "error": "No se pudo obtener el URN del autor desde userinfo ni /v2/me",
            "status_code": me_response.status_code,
            "data": me_response.json()
        }), me_response.status_code

    body = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }

    response = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={**_get_headers(access_token), "Content-Type": "application/json"},
        json=body,
        timeout=20
    )
    return jsonify({
        "author": author_urn,
        "status_code": response.status_code,
        "data": response.json() if response.text else {}
    }), response.status_code

if __name__ == "__main__":
    app.run(port=8000)