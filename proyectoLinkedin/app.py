from flask import Flask, request
import requests

app = Flask(__name__)

# 🔑 Tus credenciales
REDIRECT_URI = "http://localhost:8000/callback"
CLIENT_ID = "776gggs6opxa6u"
CLIENT_SECRET = "WPL_AP1.ErSO7s4Tc449hgYu.w+Aevw=="

@app.route("/callback")
def callback():
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

    print(f"🔑 ACCESS TOKEN: {access_token}")

    # 📡 Llamada a la API de LinkedIn
    api_url = "https://api.linkedin.com/v2/userinfo"

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

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
    """

if __name__ == "__main__":
    app.run(port=8000)