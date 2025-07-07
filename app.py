import os
import json
import pandas as pd
import requests
from flask import Flask, request, render_template, session, redirect, url_for, jsonify, send_from_directory, send_file
from werkzeug.utils import safe_join
from fpdf import FPDF
import qrcode
from datetime import datetime
import uuid # For generating unique IDs

# Import Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore, auth
import base64 # Added for decoding service account key

app = Flask(__name__) # Renamed back to 'app'
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dev_only_change_this_in_prod')

# --- Firebase Initialization ---
# These global variables are provided by the Canvas environment.
# If running locally, you'll need to replace them with your Firebase project config.

# Check if running in Canvas environment (where __firebase_config is defined)
if '__firebase_config' in globals() and '__initial_auth_token' in globals():
    firebase_config = json.loads(__firebase_config)
    initial_auth_token = __initial_auth_token
    app_id = __app_id # Canvas provides a unique app ID
    print("Running in Canvas environment. Firebase config loaded from globals.")
else:
    # Fallback for local development/production if globals are not set
    print("Running in local development/production environment. Using provided Firebase config.")
    # >>> IMPORTANT: Replace with your actual Firebase project config <<<
    # User provided config (from your Firebase Console):
    firebase_config = {
        "apiKey": "AIzaSyAV4lMNtuJSsjzo61OVVEZ56zUDcqIQtnw",
        "authDomain": "database-df336.firebaseapp.com",
        "projectId": "database-df336",
        "storageBucket": "database-df336.firebasestorage.app",
        "messagingSenderId": "1009503352682",
        "appId": "1:1009503352682:web:a86c5b028b4e1db2c6d713",
        "measurementId": "G-5P67JNH8J4" # measurementId is optional for Admin SDK
    }
    initial_auth_token = None # Not used for local/server-side auth with Admin SDK
    app_id = firebase_config['projectId'] # Use project ID as app ID for local/server testing

    # For production, load service account key from environment variable (base64 encoded)
    if os.environ.get('FIREBASE_SERVICE_ACCOUNT_BASE64'):
        try:
            service_account_json_str = base64.b64decode(os.environ['FIREBASE_SERVICE_ACCOUNT_BASE64']).decode('utf-8')
            service_account_dict = json.loads(service_account_json_str)
            cred = credentials.Certificate(service_account_dict)
            firebase_admin.initialize_app(cred, {'projectId': firebase_config['projectId']})
            print("Firebase Admin SDK initialized from environment variable.")
        except Exception as e:
            print(f"ERROR: Failed to initialize Firebase Admin SDK from environment variable: {e}")
            print("Please ensure FIREBASE_SERVICE_ACCOUNT_BASE64 is correctly set and base64 encoded JSON.")
    else:
        # Fallback to local file if env var not found (for local dev only, NOT for production server)
        firebase_admin_credentials_path = "serviceAccountKey.json" 
        try:
            cred = credentials.Certificate(firebase_admin_credentials_path)
            firebase_admin.initialize_app(cred, {'projectId': firebase_config['projectId']})
            print(f"Firebase Admin SDK initialized locally using {firebase_admin_credentials_path}")
            print("WARNING: Using local 'serviceAccountKey.json'. For production, use environment variable FIREBASE_SERVICE_ACCOUNT_BASE64.")
        except FileNotFoundError:
            print(f"ERROR: Firebase service account key not found at '{firebase_admin_credentials_path}'. Firestore features will not work.")
            print("For production, set FIREBASE_SERVICE_ACCOUNT_BASE64 environment variable.")
        except Exception as e:
            print(f"ERROR: Could not initialize Firebase Admin SDK locally: {e}")

# If Firebase Admin SDK was not initialized due to errors above, db will not work.
# We proceed to get the client, but operations will fail if init failed.
db = firestore.client()

# --- Configuration ---
UPLOAD_FOLDER = 'uploads'
RESPONSE_FOLDER = 'responses'
JSON_FOLDER = 'json' # This folder is for the JSON payload *sent* to FBR
PDF_FOLDER = 'pdf'
QR_FOLDER = 'qr'
# TEMP_DATA_FOLDER is not typically needed for web hosting as sessions handle data transiently

# FBR API Endpoints (Sandbox URLs - adjust for production if needed)
FBR_VALIDATE_URL = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata_sb"
FBR_POST_URL = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata_sb"

# --- Create necessary directories ---
# These directories should be created on the server where the app is deployed
for folder in [UPLOAD_FOLDER, RESPONSE_FOLDER, JSON_FOLDER, PDF_FOLDER, QR_FOLDER]:
    os.makedirs(folder, exist_ok=True)
    print(f"Ensured directory exists: {folder}")

# --- Helper Functions ---

def safe_str_strip(value):
    """Safely converts a value to string and strips whitespace."""
    return str(value).strip() if pd.notna(value) else ""

def to_float(val):
    """Safely converts a value to float, defaulting to 0.0 for non-numeric."""
    try:
        return float(val) if pd.notna(val) else 0.0
    except (ValueError, TypeError):
        return 0.0

def format_hs_code_for_fbr(hs_code_raw):
    """
    Attempts to format HS Code to FBR's common XXXX.YYYY format (8 characters).
    If the raw HS code is numeric-like, it will attempt to format it.
    Otherwise, it will return the stripped string.
    """
    hs_code_str = safe_str_strip(hs_code_raw)
    
    # Try to convert to float to see if it's a number, then format
    try:
        # Remove existing dots for consistent formatting
        numeric_part = hs_code_str.replace('.', '')
        # Ensure it's at least 8 digits for consistent formatting
        if len(numeric_part) < 8:
            numeric_part = numeric_part.ljust(8, '0') # Pad with trailing zeros
        
        # Take the first 8 characters and insert decimal
        return f"{numeric_part[:4]}.{numeric_part[4:8]}"
    except ValueError:
        # If not a simple number, return as is (stripped string)
        return hs_code_str.replace('.', '') # Remove any existing dots if it's not a standard numeric format

async def get_firestore_user_id():
    """Authenticates user and returns user ID for Firestore operations."""
    current_user_id = session.get('user_id')
    if current_user_id:
        return current_user_id

    try:
        if initial_auth_token:
            # Sign in with custom token provided by Canvas
            decoded_token = auth.verify_id_token(initial_auth_token)
            user_id = decoded_token['uid']
        else:
            # Fallback for local development/production: generate a random UUID for user_id.
            # For multi-user scenarios on a hosted app, you'd integrate client-side Firebase Auth
            # (e.g., Google Sign-In) to get a persistent user ID.
            user_id = str(uuid.uuid4())
            print("WARNING: No initial auth token. Using a random UUID as user_id for this session. This is not persistent or secure across sessions/users.")
        
        session['user_id'] = user_id
        return user_id
    except Exception as e:
        print(f"Firebase authentication failed: {e}")
        session['user_id'] = str(uuid.uuid4()) # Fallback to random UUID if auth fails
        return session['user_id']


# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
async def upload_file(): # Made async to await get_firestore_user_id
    """
    Handles the initial Excel file upload.
    Parses the Excel into a list of invoice dictionaries and stores them in the session.
    Also checks Firestore to skip already posted invoices.
    Redirects to the /invoices route to display the table.
    """
    if request.method == 'POST':
        token = request.form.get("token")
        if not token:
            return render_template("index.html", messages=["❌ Authorization Token is required."]), 400

        file = request.files.get('file')
        if not file or file.filename == '':
            return render_template("index.html", messages=["❌ No file uploaded or file name is empty."]), 400

        try:
            df = pd.read_excel(file)
        except Exception as e:
            return render_template("index.html", messages=[f"❌ Error reading Excel file: {e}"]), 400

        if 'SINV' not in df.columns:
            return render_template("index.html", messages=["❌ Excel must include 'SINV' column."]), 400

        session['fbr_token'] = token
        
        user_id = await get_firestore_user_id()
        print(f"Authenticated user ID: {user_id}, App ID: {app_id}")

        processed_invoices_data = []
        messages = []
        invoices_grouped = df.groupby('SINV')

        # Fetch already posted invoices from Firestore
        # Collection path: /artifacts/{appId}/users/{userId}/posted_invoices
        posted_invoices_ref = db.collection('artifacts').document(app_id).collection('users').document(user_id).collection('posted_invoices')
        try:
            # Refined query: Only fetch documents that have a 'fbr_invoice_number' field that is not 'N/A'
            # This requires a Firestore index on 'fbr_invoice_number' if you don't have one.
            # Firestore console will provide a link to create it if missing.
            posted_docs_query = posted_invoices_ref.where('fbr_invoice_number', '!=', 'N/A').stream()
            already_posted_sinvs = {doc.id for doc in posted_docs_query}
            print(f"Found {len(already_posted_sinvs)} already posted invoices in Firestore (with FBR Invoice No.).")
        except Exception as e:
            messages.append(f"❌ Error fetching posted invoices from Firestore: {e}. Proceeding without skipping based on FBR Invoice No.")
            print(f"DEBUG: Error fetching posted invoices from Firestore: {e}")
            already_posted_sinvs = set()


        for sinv, group in invoices_grouped:
            sinv_str = str(sinv) # Ensure SINV is a string for consistent keys/IDs
            
            # Check if this invoice was already posted AND has a valid FBR Invoice Number
            if sinv_str in already_posted_sinvs:
                messages.append(f"SINV {sinv_str}: ℹ️ Skipped (Already Posted).")
                processed_invoices_data.append({
                    "sinv": sinv_str,
                    "invoice_json": {}, # Empty JSON as we're skipping processing
                    "status": "Skipped (Already Posted)",
                    "fbr_invoice_number": "N/A", # This will be overwritten if fetched from DB later
                    "qr_code_url": "",
                    "validation_response_text": "Invoice previously posted.",
                    "post_response_text": "Invoice previously posted.",
                    "validation_response_file_url": ""
                })
                continue # Skip to next invoice in Excel

            try:
                invoice_header = group.iloc[0].to_dict()
                
                invoice_date_raw = invoice_header.get("invoiceDate")
                if pd.isna(invoice_date_raw):
                    messages.append(f"SINV {sinv_str}: ❌ Error: 'invoiceDate' is missing or invalid. Skipping.")
                    continue
                
                try:
                    invoice_date = pd.to_datetime(invoice_date_raw).strftime('%Y-%m-%d')
                except Exception as date_e:
                    messages.append(f"SINV {sinv_str}: ❌ Error parsing 'invoiceDate': {date_e}. Raw value: {invoice_date_raw}. Skipping.")
                    continue

                invoice_json = {
                    "invoiceType": safe_str_strip(invoice_header.get("invoiceType", "Sale Invoice")),
                    "invoiceDate": invoice_date,
                    "sellerNTNCNIC": safe_str_strip(invoice_header.get("sellerNTNCNIC", "")), 
                    "sellerBusinessName": safe_str_strip(invoice_header.get("sellerBusinessName", "")),
                    "sellerProvince": safe_str_strip(invoice_header.get("sellerProvince", "")),
                    "sellerAddress": safe_str_strip(invoice_header.get("sellerAddress", "")),
                    "buyerNTNCNIC": safe_str_strip(invoice_header.get("buyerNTNCNIC", "")),
                    "buyerBusinessName": safe_str_strip(invoice_header.get("buyerBusinessName", "")),
                    "buyerProvince": safe_str_strip(invoice_header.get("buyerProvince", "")),
                    "buyerAddress": safe_str_strip(invoice_header.get("buyerAddress", "")),
                    "buyerRegistrationType": safe_str_strip(invoice_header.get("buyerRegistrationType", "Registered")),
                    "invoiceRefNo": safe_str_strip(invoice_header.get("invoiceRefNo", "")),
                    "scenarioId": safe_str_strip(invoice_header.get("scenarioId", "SN001")),
                    "items": []
                }

                for item_row in group.to_dict('records'):
                    rate_val = item_row.get("rate")
                    if isinstance(rate_val, str) and rate_val.endswith('%'):
                        try:
                            rate = f"{int(rate_val.replace('%', ''))}%"
                        except ValueError:
                            messages.append(f"SINV {sinv_str}: ❌ Error: Invalid 'rate' format for item. Found: {rate_val}. Skipping item.")
                            continue
                    else:
                        try:
                            rate = f"{int(rate_val)}%" if pd.notna(rate_val) else "0%"
                        except (ValueError, TypeError):
                            messages.append(f"SINV {sinv_str}: ❌ Error: Invalid 'rate' format for item. Found: {rate_val}. Skipping item.")
                            continue

                    item_obj = {
                        "hsCode": format_hs_code_for_fbr(item_row.get("hsCode", "")),
                        "productDescription": safe_str_strip(item_row.get("productDescription", "")),
                        "rate": rate,
                        "uoM": safe_str_strip(item_row.get("uoM", "")),
                        "quantity": to_float(item_row.get("quantity", 0)),
                        "totalValues": to_float(item_row.get("totalValues", 0)),
                        "valueSalesExcludingST": to_float(item_row.get("valueSalesExcludingST", 0)),
                        "fixedNotifiedValueOrRetailPrice": to_float(item_row.get("fixedNotifiedValueOrRetailPrice", 0)),
                        "salesTaxApplicable": to_float(item_row.get("salesTaxApplicable", 0)),
                        "salesTaxWithheldAtSource": to_float(item_row.get("salesTaxWithheldAtSource", 0)),
                        "extraTax": safe_str_strip(item_row.get("extraTax", "")),
                        "furtherTax": to_float(item_row.get("furtherTax", 0)),
                        "sroScheduleNo": safe_str_strip(item_row.get("sroScheduleNo", "")),
                        "fedPayable": to_float(item_row.get("fedPayable", 0)),
                        "discount": to_float(item_row.get("discount", 0)),
                        "saleType": safe_str_strip(item_row.get("saleType", "Goods at standard rate (default)")),
                        "sroItemSerialNo": safe_str_strip(item_row.get("sroItemSerialNo", ""))
                    }
                    invoice_json["items"].append(item_obj)
                
                json_path = os.path.join(JSON_FOLDER, f"{sinv_str}.json")
                with open(json_path, 'w') as f:
                    json.dump(invoice_json, f, indent=2)
                messages.append(f"SINV {sinv_str}: JSON payload prepared and saved to {json_path}")
                print(f"DEBUG: Saved JSON payload to {json_path}")

                processed_invoices_data.append({
                    "sinv": sinv_str,
                    "invoice_json": invoice_json,
                    "status": "Pending",
                    "fbr_invoice_number": "",
                    "qr_code_url": "",
                    "validation_response_text": "",
                    "post_response_text": "",
                    "validation_response_file_url": ""
                })

            except Exception as e:
                messages.append(f"SINV {sinv_str}: ❌ Error processing invoice data from Excel: {e}")
                print(f"DEBUG: Error processing SINV {sinv_str} from Excel: {e}")


        session['invoices_data'] = processed_invoices_data
        session['initial_messages'] = messages

        return redirect(url_for('display_invoices'))
    
    return render_template("index.html", messages=[])

@app.route('/invoices')
def display_invoices():
    if 'invoices_data' not in session:
        return redirect(url_for('upload_file'))

    invoices = session.get('invoices_data', [])
    initial_messages = session.pop('initial_messages', [])

    return render_template("invoices.html", invoices=invoices, initial_messages=initial_messages)

@app.route('/api/validate_invoice/<sinv>', methods=['POST'])
async def api_validate_invoice(sinv): # Made async
    token = session.get('fbr_token')
    if not token:
        return jsonify({"status": "error", "message": "Authorization token missing. Please re-upload Excel."}), 401

    invoices_data = session.get('invoices_data', [])
    invoice_found = None
    for inv in invoices_data:
        if inv['sinv'] == sinv:
            invoice_found = inv
            break
    
    if not invoice_found:
        return jsonify({"status": "error", "message": f"Invoice {sinv} not found."}), 404

    invoice_json = invoice_found['invoice_json']
    
    try:
        request_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
        
        print(f"\n--- SINV {sinv} Validation Request ---")
        print(f"URL: {FBR_VALIDATE_URL}")
        print(f"Headers: {json.dumps(request_headers, indent=2)}")
        print(f"Request Body (JSON): {json.dumps(invoice_json, indent=2)}")
        print(f"--- End SINV {sinv} Validation Request ---\n")

        validate_response = requests.post(
            FBR_VALIDATE_URL,
            headers=request_headers,
            json=invoice_json,
            timeout=30
        )
        print(f"SINV {sinv}: Validation Response Status: {validate_response.status_code}")
        print(f"SINV {sinv}: Validation Response Body: {validate_response.text}")

        validation_response_filename = f"{sinv}_validated.json"
        validation_response_path = os.path.join(RESPONSE_FOLDER, validation_response_filename)
        try:
            with open(validation_response_path, 'w') as f:
                json.dump(validate_response.json(), f, indent=2)
            print(f"SINV {sinv}: FBR validation response saved to {validation_response_path}")
        except json.JSONDecodeError:
            with open(validation_response_path, 'w') as f:
                f.write(validate_response.text)
            print(f"SINV {sinv}: FBR validation response saved as plain text to {validation_response_path} (not valid JSON)")
        except Exception as save_e:
            print(f"SINV {sinv}: Error saving validation response file: {save_e}")


        is_valid = False
        fbr_message = ""
        try:
            validate_res_json = validate_response.json()
            validation_response_data = validate_res_json.get("validationResponse", {})
            status_from_fbr_inner = validation_response_data.get("status", "").lower()

            if status_from_fbr_inner == "valid":
                is_valid = True
                fbr_message = "Validation successful."
            elif status_from_fbr_inner == "invalid":
                fbr_message = validation_response_data.get("error", "Validation failed, no specific error provided.")
            else:
                fbr_message = f"Unexpected FBR status: '{status_from_fbr_inner}'. Full response: {validate_response.text}"

        except json.JSONDecodeError:
            fbr_message = f"FBR Validation Response is not valid JSON. Raw: {validate_response.text}"
        except Exception as parse_e:
            fbr_message = f"Error parsing FBR Validation Response: {parse_e}. Raw: {validate_response.text}"

        for inv in invoices_data:
            if inv['sinv'] == sinv:
                inv['status'] = "Valid" if is_valid else "Validation Failed"
                inv['validation_response_text'] = fbr_message
                inv['validation_response_file_url'] = url_for('view_response_file', filename=validation_response_filename)
                break
        session['invoices_data'] = invoices_data

        return jsonify({
            "status": "success" if is_valid else "validation_failed",
            "message": fbr_message,
            "sinv": sinv,
            "can_post": is_valid,
            "validation_response_file_url": url_for('view_response_file', filename=validation_response_filename)
        })

    except requests.exceptions.RequestException as req_e:
        return jsonify({"status": "error", "message": f"Network/API Request Error: {req_e}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500

@app.route('/api/post_invoice/<sinv>', methods=['POST'])
async def api_post_invoice(sinv): # Made async
    token = session.get('fbr_token')
    if not token:
        return jsonify({"status": "error", "message": "Authorization token missing. Please re-upload Excel."}), 401

    invoices_data = session.get('invoices_data', [])
    invoice_found = None
    for inv in invoices_data:
        if inv['sinv'] == sinv:
            invoice_found = inv
            break
    
    if not invoice_found:
        return jsonify({"status": "error", "message": f"Invoice {sinv} not found."}), 404

    if invoice_found['status'] != "Valid":
        return jsonify({"status": "error", "message": f"Invoice {sinv} must be successfully validated before posting."}), 400

    invoice_json = invoice_found['invoice_json']

    try:
        request_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        print(f"\n--- SINV {sinv} Post Request ---")
        print(f"URL: {FBR_POST_URL}")
        print(f"Headers: {json.dumps(request_headers, indent=2)}")
        print(f"Request Body (JSON): {json.dumps(invoice_json, indent=2)}")
        print(f"--- End SINV {sinv} Post Request ---\n")

        post_response = requests.post(
            FBR_POST_URL,
            headers=request_headers,
            json=invoice_json,
            timeout=30
        )
        print(f"SINV {sinv}: Post Response Status: {post_response.status_code}")
        print(f"SINV {sinv}: Post Response Body: {post_response.text}")

        fbr_invoice_number = "N/A"
        qr_code_url = ""
        pdf_url = ""
        post_message = ""
        
        res_json = {}
        try:
            res_json = post_response.json()
            fbr_invoice_number = res_json.get("invoiceNumber", "N/A")
            post_message = "Invoice posted successfully."
        except json.JSONDecodeError:
            post_message = f"FBR Post Response is not valid JSON. Raw: {post_response.text}"
        except Exception as parse_e:
            post_message = f"Error parsing FBR Post Response: {parse_e}. Raw: {post_response.text}"

        response_path = os.path.join(RESPONSE_FOLDER, f"{sinv}_posted.json")
        with open(response_path, 'w') as f:
            json.dump(res_json, f, indent=2)

        if fbr_invoice_number != "N/A":
            # --- Save to Firestore after successful FBR Post ---
            try:
                user_id = await get_firestore_user_id()
                invoice_doc_ref = db.collection('artifacts').document(app_id).collection('users').document(user_id).collection('posted_invoices').document(sinv)
                invoice_doc_ref.set({
                    'sinv': sinv,
                    'fbr_invoice_number': fbr_invoice_number,
                    'post_date': firestore.SERVER_TIMESTAMP,
                    'status': 'posted'
                })
                print(f"DEBUG: SINV {sinv} marked as posted in Firestore.")
                post_message += " (Status saved to Firestore)"
            except Exception as firestore_e:
                post_message += f" (WARNING: Failed to save status to Firestore: {firestore_e})"
                print(f"ERROR: Failed to save SINV {sinv} to Firestore: {firestore_e}")

            # Generate QR Code
            qr_data = (
                f"Invoice No: {fbr_invoice_number}\n"
                f"Seller: {invoice_json['sellerBusinessName']}\n"
                f"Buyer: {invoice_json['buyerBusinessName']}\n"
                f"Date: {invoice_json['invoiceDate']}"
            )
            qr = qrcode.make(qr_data)
            qr_filename = f"{sinv}_{fbr_invoice_number}.png"
            qr_path = os.path.join(QR_FOLDER, qr_filename)
            qr.save(qr_path)
            qr_code_url = url_for('download_qr', filename=qr_filename)

            # Generate PDF (can be done on demand or here)
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=12)
            pdf.cell(200, 10, txt=f"Invoice No: {fbr_invoice_number}", ln=True)
            pdf.cell(200, 10, txt=f"Seller: {invoice_json['sellerBusinessName']}", ln=True)
            pdf.cell(200, 10, txt=f"Buyer: {invoice_json['buyerBusinessName']}", ln=True)
            pdf.cell(200, 10, txt=f"Date: {invoice_json['invoiceDate']}", ln=True)
            
            pdf.ln(10)
            pdf.set_font("Arial", 'B', size=10)
            pdf.cell(50, 7, "Product", 1)
            pdf.cell(30, 7, "Quantity", 1)
            pdf.cell(30, 7, "Rate", 1)
            pdf.cell(40, 7, "Sales Tax", 1)
            pdf.cell(40, 7, "Total Value", 1, ln=True)
            pdf.set_font("Arial", size=10)
            for item in invoice_json["items"]:
                pdf.cell(50, 7, item.get("productDescription", ""), 1)
                pdf.cell(30, 7, str(item.get("quantity", "")), 1)
                pdf.cell(30, 7, str(item.get("rate", "")), 1)
                pdf.cell(40, 7, str(item.get("salesTaxApplicable", "")), 1)
                pdf.cell(40, 7, str(item.get("totalValues", "")), 1, ln=True)

            pdf.ln(10)
            if os.path.exists(qr_path):
                pdf.image(qr_path, x=10, y=pdf.get_y(), w=40)
                pdf.ln(45)
            
            pdf_filename = f"{sinv}_{fbr_invoice_number}.pdf"
            pdf_path = os.path.join(PDF_FOLDER, pdf_filename)
            pdf.output(pdf_path)
            pdf_url = url_for('download_pdf', filename=pdf_filename)
            post_message += f" PDF generated at {pdf_url}"
        else:
            post_message += " Invoice number missing or invalid from FBR response."

        for inv in invoices_data:
            if inv['sinv'] == sinv:
                inv['status'] = "Posted" if fbr_invoice_number != "N/A" else "Post Failed"
                inv['fbr_invoice_number'] = fbr_invoice_number
                inv['qr_code_url'] = qr_code_url
                inv['pdf_url'] = pdf_url
                inv['post_response_text'] = post_message
                break
        session['invoices_data'] = invoices_data

        return jsonify({
            "status": "success" if fbr_invoice_number != "N/A" else "post_failed",
            "message": post_message,
            "sinv": sinv,
            "fbr_invoice_number": fbr_invoice_number,
            "qr_code_url": qr_code_url,
            "pdf_url": pdf_url
        })

    except requests.exceptions.RequestException as req_e:
        return jsonify({"status": "error", "message": f"Network/API Request Error: {req_e}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500

@app.route('/downloads/qr/<filename>')
def download_qr(filename):
    return send_from_directory(QR_FOLDER, filename, as_attachment=True)

@app.route('/downloads/pdf/<filename>')
def download_pdf(filename):
    return send_from_directory(PDF_FOLDER, filename, as_attachment=True)

@app.route('/view/response_file/<filename>')
def view_response_file(filename):
    file_path = safe_join(app.root_path, RESPONSE_FOLDER, filename)
    
    if filename.endswith('.json'):
        mimetype = 'application/json'
    else:
        mimetype = 'text/plain'

    return send_file(file_path, mimetype=mimetype)


# --- Main Application Entry Point for Web Hosting ---
if __name__ == '__main__':
    # This block is for local development only.
    # When deployed with Gunicorn/Nginx, they will run `app` directly.
    print("Running Flask app in local development mode.")
    app.run(debug=True, port=5000) # Use a standard port for local testing
