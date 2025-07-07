import sys
import os

# Add your project directory to the Python path
# Replace 'your_app_name' with the actual folder name you'll create on cPanel
# Example: /home/youruser/fbr_invoice_app
sys.path.insert(0, os.path.dirname(__file__))

# Activate the virtual environment
# Replace 'venv' if you use a different name for your virtual environment
INTERP = os.path.join(os.path.dirname(__file__), 'venv', 'bin', 'python')
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

# Import your Flask application instance
# Assuming your Flask app instance is named 'app' in app.py
from app import app as application