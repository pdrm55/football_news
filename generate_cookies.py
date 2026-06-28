import json
import os

def main():
    print("=" * 60)
    print("         X (TWITTER) COOKIE GENERATOR UTILITY        ")
    print("=" * 60)
    print("To bypass Twitter's automatic login blocks, you can export your cookies.")
    print("Follow these steps:")
    print("1. Open x.com in your web browser and log in.")
    print("2. Press F12 (Inspect Element) and navigate to the 'Application' tab (Chrome) or 'Storage' tab (Firefox).")
    print("3. Expand 'Cookies' and click on 'https://x.com'.")
    print("4. Copy the values of the following cookies:")
    print("   - 'auth_token'")
    print("   - 'ct0'")
    print("-" * 60)

    auth_token = input("Enter 'auth_token' value: ").strip()
    ct0 = input("Enter 'ct0' value: ").strip()

    if not auth_token or not ct0:
        print("❌ Error: Both auth_token and ct0 are required.")
        return

    # Construct the cookies dict in the format Twikit expects
    cookies = {
        "auth_token": auth_token,
        "ct0": ct0
    }

    cookies_path = "cookies.json"
    try:
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=4)
        print(f"\n✅ SUCCESS: '{cookies_path}' generated successfully!")
        print("Now you can run 'test_single_x.py' or start the bot to scrape X live.")
    except Exception as e:
        print(f"❌ Failed to write cookies file: {e}")

if __name__ == "__main__":
    main()
