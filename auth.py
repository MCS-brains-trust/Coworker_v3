"""
One-time Xero authorisation. Run this once before using main.py.
A browser window will open — log in to Xero and approve access.
The token is saved to xero_token.json and reused automatically from then on.
"""

from xero_auth import get_access_token

if __name__ == "__main__":
    token, tenant = get_access_token()
    print(f"\nAuthorised. Tenant ID: {tenant}")
    print("You can now run: python main.py")
