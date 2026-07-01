"""
NasrTrading Backend — Flask + Supabase + PayPal Webhooks
"""
import os
import uuid
import hashlib
import hmac
import json
import random
import string
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client
import requests

app = Flask(__name__)
CORS(app, origins=["https://nasrtrading.netlify.app", "http://localhost:3000"])

# ─── Supabase ─────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ygfakkkiguzsmbgyegnt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── PayPal ───────────────────────────────────────────────────────
PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID    = os.environ.get("PAYPAL_WEBHOOK_ID")
PAYPAL_BASE = "https://api-m.paypal.com"  # live
# PAYPAL_BASE = "https://api-m.sandbox.paypal.com"  # sandbox للتجربة

# ─── الأسعار ──────────────────────────────────────────────────────
PLANS = {
    "monthly":  {"amount": "90.00",  "currency": "USD", "days": 30},
    "lifetime": {"amount": "200.00", "currency": "USD", "days": 36500},
}

# ─── مساعدات ──────────────────────────────────────────────────────

def generate_license():
    """توليد كود ترخيص عشوائي"""
    parts = [''.join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(4)]
    return '-'.join(parts)

def get_paypal_token():
    """الحصول على access token من PayPal"""
    resp = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"}
    )
    return resp.json().get("access_token")

def verify_paypal_webhook(headers, body):
    """التحقق من صحة Webhook من PayPal"""
    token = get_paypal_token()
    verify_resp = requests.post(
        f"{PAYPAL_BASE}/v1/notifications/verify-webhook-signature",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "auth_algo":         headers.get("PAYPAL-AUTH-ALGO"),
            "cert_url":          headers.get("PAYPAL-CERT-URL"),
            "transmission_id":   headers.get("PAYPAL-TRANSMISSION-ID"),
            "transmission_sig":  headers.get("PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
            "webhook_id":        PAYPAL_WEBHOOK_ID,
            "webhook_event":     json.loads(body),
        }
    )
    return verify_resp.json().get("verification_status") == "SUCCESS"

# ─── Routes ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({"status": "NasrTrading Backend يعمل ✅", "version": "1.0"})

@app.route("/api/create-order", methods=["POST"])
def create_order():
    """إنشاء طلب دفع PayPal"""
    data = request.json
    plan  = data.get("plan")
    email = data.get("email", "").strip().lower()

    if plan not in PLANS:
        return jsonify({"error": "خطة غير صحيحة"}), 400
    if not email:
        return jsonify({"error": "البريد الإلكتروني مطلوب"}), 400

    token = get_paypal_token()
    plan_info = PLANS[plan]

    order_resp = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": plan_info["currency"],
                    "value": plan_info["amount"]
                },
                "description": f"NasrTrading {plan.capitalize()} Plan",
                "custom_id": f"{plan}|{email}"
            }],
            "application_context": {
                "return_url": "https://nasrtrading.netlify.app/success.html",
                "cancel_url": "https://nasrtrading.netlify.app/cancel.html",
                "brand_name": "NasrTrading",
                "landing_page": "BILLING",
                "user_action": "PAY_NOW"
            }
        }
    )

    order = order_resp.json()
    if "id" not in order:
        return jsonify({"error": "فشل إنشاء الطلب", "details": order}), 500

    # احفظ الطلب في قاعدة البيانات
    try:
        # تحقق من المستخدم أو أنشئه
        user_result = supabase.table("users").select("id").eq("email", email).execute()
        if user_result.data:
            user_id = user_result.data[0]["id"]
        else:
            new_user = supabase.table("users").insert({"email": email}).execute()
            user_id = new_user.data[0]["id"]

        # احفظ الدفع
        supabase.table("payments").insert({
            "user_id": user_id,
            "amount": float(plan_info["amount"]),
            "plan": plan,
            "paypal_order_id": order["id"],
            "status": "pending"
        }).execute()
    except Exception as e:
        print(f"Supabase error: {e}")

    # رابط الدفع
    approve_url = next((l["href"] for l in order.get("links", []) if l["rel"] == "approve"), None)
    return jsonify({"order_id": order["id"], "approve_url": approve_url})


@app.route("/api/capture-order", methods=["POST"])
def capture_order():
    """تأكيد الدفع وإنشاء الترخيص"""
    data     = request.json
    order_id = data.get("order_id")

    if not order_id:
        return jsonify({"error": "order_id مطلوب"}), 400

    token = get_paypal_token()
    capture_resp = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    capture = capture_resp.json()

    if capture.get("status") != "COMPLETED":
        return jsonify({"error": "الدفع لم يكتمل", "details": capture}), 400

    # استخرج البيانات
    unit = capture["purchase_units"][0]
    custom = unit.get("custom_id", "|")
    plan, email = custom.split("|") if "|" in custom else ("monthly", "")
    amount = float(unit["payments"]["captures"][0]["amount"]["value"])

    return _activate_license(email, plan, amount, order_id)


@app.route("/api/paypal-webhook", methods=["POST"])
def paypal_webhook():
    """استقبال Webhook من PayPal"""
    body = request.get_data(as_text=True)

    # تحقق من صحة الـ Webhook
    if not verify_paypal_webhook(dict(request.headers), body):
        return jsonify({"error": "Webhook غير موثوق"}), 401

    event = json.loads(body)
    event_type = event.get("event_type")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        resource = event["resource"]
        custom   = resource.get("custom_id", "|")
        plan, email = custom.split("|") if "|" in custom else ("monthly", "")
        amount   = float(resource["amount"]["value"])
        order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id", "")

        _activate_license(email, plan, amount, order_id)

    return jsonify({"received": True})


def _activate_license(email, plan, amount, order_id):
    """تفعيل الترخيص بعد الدفع"""
    try:
        plan_info   = PLANS.get(plan, PLANS["monthly"])
        license_key = generate_license()
        expires_at  = datetime.utcnow() + timedelta(days=plan_info["days"])

        # احصل على المستخدم
        user_result = supabase.table("users").select("id").eq("email", email).execute()
        if user_result.data:
            user_id = user_result.data[0]["id"]
        else:
            new_user = supabase.table("users").insert({"email": email}).execute()
            user_id = new_user.data[0]["id"]

        # أنشئ الترخيص
        supabase.table("licenses").insert({
            "user_id":     user_id,
            "license_key": license_key,
            "plan":        plan,
            "status":      "active",
            "expires_at":  expires_at.isoformat()
        }).execute()

        # حدّث الدفع
        supabase.table("payments").update({
            "status": "completed",
            "user_id": user_id
        }).eq("paypal_order_id", order_id).execute()

        return jsonify({
            "success":     True,
            "license_key": license_key,
            "plan":        plan,
            "expires_at":  expires_at.isoformat(),
            "message":     f"تم تفعيل اشتراك {plan} بنجاح!"
        })

    except Exception as e:
        print(f"License activation error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/verify-license", methods=["POST"])
def verify_license():
    """التحقق من صحة الترخيص"""
    data        = request.json
    license_key = data.get("license_key", "").strip().upper()

    if not license_key:
        return jsonify({"valid": False, "error": "الكود فارغ"}), 400

    result = supabase.table("licenses").select("*").eq("license_key", license_key).execute()

    if not result.data:
        return jsonify({"valid": False, "error": "الكود غير صحيح"}), 404

    lic = result.data[0]

    if lic["status"] != "active":
        return jsonify({"valid": False, "error": "الاشتراك منتهي"}), 403

    if lic["expires_at"]:
        expires = datetime.fromisoformat(lic["expires_at"].replace("Z", ""))
        if expires < datetime.utcnow():
            supabase.table("licenses").update({"status": "expired"}).eq("id", lic["id"]).execute()
            return jsonify({"valid": False, "error": "انتهت صلاحية الاشتراك"}), 403

    return jsonify({
        "valid":      True,
        "plan":       lic["plan"],
        "expires_at": lic["expires_at"],
        "message":    "الترخيص صالح ✅"
    })


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    """إحصائيات لوحة الإدارة"""
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key != os.environ.get("ADMIN_KEY"):
        return jsonify({"error": "غير مصرح"}), 401

    users    = supabase.table("users").select("id", count="exact").execute()
    licenses = supabase.table("licenses").select("*").execute()
    payments = supabase.table("payments").select("*").eq("status", "completed").execute()

    active   = [l for l in licenses.data if l["status"] == "active"]
    expired  = [l for l in licenses.data if l["status"] == "expired"]
    revenue  = sum(p["amount"] for p in payments.data)

    monthly  = [l for l in active if l["plan"] == "monthly"]
    lifetime = [l for l in active if l["plan"] == "lifetime"]

    return jsonify({
        "total_users":     users.count,
        "active_licenses": len(active),
        "expired_licenses":len(expired),
        "monthly_subs":    len(monthly),
        "lifetime_subs":   len(lifetime),
        "total_revenue":   round(revenue, 2),
        "total_payments":  len(payments.data),
    })


@app.route("/api/admin/licenses", methods=["GET"])
def admin_licenses():
    """قائمة التراخيص"""
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key != os.environ.get("ADMIN_KEY"):
        return jsonify({"error": "غير مصرح"}), 401

    result = supabase.table("licenses").select("*, users(email)").order("created_at", desc=True).execute()
    return jsonify(result.data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
