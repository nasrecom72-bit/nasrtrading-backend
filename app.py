"""
NasrTrading Backend — Flask + Supabase REST API + PayPal
"""
import os
import uuid
import json
import random
import string
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app, origins=["https://nasrtrading.netlify.app", "http://localhost:3000"])

# ─── Config ───────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ygfakkkiguzsmbgyegnt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "nasrtrading2024")

PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID    = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_BASE          = "https://api-m.paypal.com"

PLANS = {
    "monthly":  {"amount": "90.00",  "currency": "USD", "days": 30},
    "lifetime": {"amount": "200.00", "currency": "USD", "days": 36500},
}

# ─── Supabase REST Helper ─────────────────────────────────────────

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def sb_get(table, filters=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    r = requests.get(url, headers=sb_headers())
    return r.json() if r.status_code < 300 else []

def sb_post(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=sb_headers(), json=data)
    result = r.json()
    return result[0] if isinstance(result, list) and result else result

def sb_patch(table, filters, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    r = requests.patch(url, headers=sb_headers(), json=data)
    return r.status_code < 300

# ─── Helpers ──────────────────────────────────────────────────────

def generate_license():
    parts = [''.join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(4)]
    return '-'.join(parts)

def get_paypal_token():
    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"}
    )
    return r.json().get("access_token", "")

def get_or_create_user(email):
    users = sb_get("users", f"email=eq.{email}&select=id")
    if users:
        return users[0]["id"]
    user = sb_post("users", {"email": email})
    return user.get("id")

# ─── Routes ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({"status": "NasrTrading Backend يعمل ✅", "version": "2.0"})

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/api/create-order", methods=["POST"])
def create_order():
    data  = request.json or {}
    plan  = data.get("plan", "")
    email = data.get("email", "").strip().lower()

    if plan not in PLANS:
        return jsonify({"error": "خطة غير صحيحة"}), 400
    if not email:
        return jsonify({"error": "البريد الإلكتروني مطلوب"}), 400
    if not PAYPAL_CLIENT_ID:
        return jsonify({"error": "PayPal غير مهيأ"}), 500

    token = get_paypal_token()
    plan_info = PLANS[plan]

    r = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": plan_info["currency"], "value": plan_info["amount"]},
                "description": f"NasrTrading {plan.capitalize()} Plan",
                "custom_id": f"{plan}|{email}"
            }],
            "application_context": {
                "return_url": "https://nasrtrading.netlify.app/success.html",
                "cancel_url": "https://nasrtrading.netlify.app/cancel.html",
                "brand_name": "NasrTrading",
                "user_action": "PAY_NOW"
            }
        }
    )
    order = r.json()
    if "id" not in order:
        return jsonify({"error": "فشل إنشاء الطلب", "details": order}), 500

    # احفظ في DB
    try:
        user_id = get_or_create_user(email)
        sb_post("payments", {
            "user_id": user_id,
            "amount": float(plan_info["amount"]),
            "plan": plan,
            "paypal_order_id": order["id"],
            "status": "pending"
        })
    except Exception as e:
        print(f"DB error: {e}")

    approve_url = next((l["href"] for l in order.get("links", []) if l["rel"] == "approve"), None)
    return jsonify({"order_id": order["id"], "approve_url": approve_url})


@app.route("/api/capture-order", methods=["POST"])
def capture_order():
    data     = request.json or {}
    order_id = data.get("order_id", "")

    if not order_id:
        return jsonify({"error": "order_id مطلوب"}), 400

    token = get_paypal_token()
    r = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    capture = r.json()

    if capture.get("status") != "COMPLETED":
        return jsonify({"error": "الدفع لم يكتمل"}), 400

    unit   = capture["purchase_units"][0]
    custom = unit.get("custom_id", "monthly|")
    plan, email = custom.split("|") if "|" in custom else ("monthly", "")
    amount = float(unit["payments"]["captures"][0]["amount"]["value"])

    return _activate_license(email, plan, amount, order_id)


@app.route("/api/paypal-webhook", methods=["POST"])
def paypal_webhook():
    event      = request.json or {}
    event_type = event.get("event_type", "")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        resource = event.get("resource", {})
        custom   = resource.get("custom_id", "monthly|")
        plan, email = custom.split("|") if "|" in custom else ("monthly", "")
        amount   = float(resource.get("amount", {}).get("value", 0))
        order_id = resource.get("id", "")
        _activate_license(email, plan, amount, order_id)

    return jsonify({"received": True})


def _activate_license(email, plan, amount, order_id):
    try:
        plan_info   = PLANS.get(plan, PLANS["monthly"])
        license_key = generate_license()
        expires_at  = (datetime.utcnow() + timedelta(days=plan_info["days"])).isoformat()

        user_id = get_or_create_user(email)

        sb_post("licenses", {
            "user_id":     user_id,
            "license_key": license_key,
            "plan":        plan,
            "status":      "active",
            "expires_at":  expires_at
        })

        sb_patch("payments", f"paypal_order_id=eq.{order_id}", {"status": "completed"})

        return jsonify({
            "success":     True,
            "license_key": license_key,
            "plan":        plan,
            "expires_at":  expires_at,
            "message":     "تم التفعيل بنجاح! 🎉"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/verify-license", methods=["POST"])
def verify_license():
    data        = request.json or {}
    license_key = data.get("license_key", "").strip().upper().replace(" ", "")

    if not license_key:
        return jsonify({"valid": False, "error": "الكود فارغ"}), 400

    results = sb_get("licenses", f"license_key=eq.{license_key}")
    if not results:
        return jsonify({"valid": False, "error": "الكود غير صحيح ❌"}), 404

    lic = results[0]
    if lic.get("status") != "active":
        return jsonify({"valid": False, "error": "الاشتراك منتهي ⏰"}), 403

    if lic.get("expires_at"):
        expires = datetime.fromisoformat(lic["expires_at"].replace("Z", ""))
        if expires < datetime.utcnow():
            sb_patch("licenses", f"id=eq.{lic['id']}", {"status": "expired"})
            return jsonify({"valid": False, "error": "انتهت صلاحية الاشتراك ⏰"}), 403

    return jsonify({
        "valid":      True,
        "plan":       lic.get("plan"),
        "expires_at": lic.get("expires_at"),
        "message":    "الترخيص صالح ✅"
    })


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "غير مصرح"}), 401

    users    = sb_get("users", "select=id")
    licenses = sb_get("licenses", "select=*")
    payments = sb_get("payments", "select=amount&status=eq.completed")

    active   = [l for l in licenses if l.get("status") == "active"]
    revenue  = sum(float(p.get("amount", 0)) for p in payments)

    return jsonify({
        "total_users":     len(users),
        "active_licenses": len(active),
        "monthly_subs":    len([l for l in active if l.get("plan") == "monthly"]),
        "lifetime_subs":   len([l for l in active if l.get("plan") == "lifetime"]),
        "total_revenue":   round(revenue, 2),
    })


@app.route("/api/admin/licenses", methods=["GET"])
def admin_licenses():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "غير مصرح"}), 401
    return jsonify(sb_get("licenses", "select=*&order=created_at.desc"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
