import fnmatch
import os


def combine_all_files(root_dir, output_file, skip_dirs=None, skip_files=None):
    """
    Combines all files except those matching skip_dirs or skip_files using wildcards.
    """
    skip_dirs = skip_dirs or []
    skip_files = skip_files or []

    # CRITICAL FIX 1: Explicitly clear/delete the old combined file if it already exists
    # This prevents old data from being appended or lingering between runs
    if os.path.exists(output_file):
        try:
            os.remove(output_file)
        except Exception as e:
            print(f"Warning: Could not clear old file: {e}")

    with open(output_file, 'w', encoding='utf-8') as outfile:
        for root, dirs, files in os.walk(root_dir):

            # Filter folders using wildcard patterns
            valid_dirs = []
            for d in dirs:
                should_skip_dir = False
                for pattern in skip_dirs:
                    if fnmatch.fnmatch(d, pattern):
                        should_skip_dir = True
                        break
                if not should_skip_dir:
                    valid_dirs.append(d)
            dirs[:] = valid_dirs

            for filename in files:
                # Always skip the output text file itself to prevent infinite parsing loops
                if filename == output_file or filename == os.path.basename(output_file):
                    continue

                # FIX 2: Check filename against exclusion patterns case-insensitively
                should_skip = False
                for pattern in skip_files:
                    if fnmatch.fnmatch(filename.lower(), pattern.lower()):
                        should_skip = True
                        break

                if should_skip:
                    continue

                file_path = os.path.join(root, filename)

                # HEADER: Writing file name and path at the top of the content
                outfile.write(f"\n{'#' * 80}\n")
                outfile.write(f"# FILE NAME: {filename}\n")
                outfile.write(f"# FULL PATH: {file_path}\n")
                outfile.write(f"{'#' * 80}\n\n")

                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as infile:
                        outfile.write(infile.read())
                except Exception as e:
                    outfile.write(f"# [Error reading file: {e}]\n")

                # Add a few line breaks between files for readability
                outfile.write("\n\n")


if __name__ == "__main__":
    target_path = r"C:\Users\DELL\IdeaProjects\SPRING-CLASS\06 DISTRIBUTED TRACING"
    result_file = "all_project_code_combined.txt"

    # Folders to ignore
    exclude_folders = [
        '.git',
        '.idea',
        '.venv',
        '__pycache__',
        'node_modules',
        'target',
        'debug',
        'backend-stg-0',
        '.*',  # Ignore hidden folders
    ]

    # Files to ignore (Now optimized for files without extensions like mvnw)
    exclude_filenames = [
        '.*',  # Ignores all hidden files (.gitignore, .env)
        '*docker*',  # Ignores any docker configurations
        '*mvnw*',  # FIX 3: Catches "mvnw", "mvnw.cmd", and maven wrappers as files
        'pom.xml',
        'h.py',
        'data.py',
        'secrets.env',
        'package.json',
        'package-lock.json',
        'sp500_tickers.csv',
        '*.md',
        '*.db',
        '*.json',
        '*.html',
        '*.txt',
        '*.png',
        '*.jpg',
        '*.ico',
        '*.class',
        '*.jar',
        '*.cmd'  # Blocks Windows command scripts often paired with maven wrappers
    ]

    print(f"Combining allowed files in {os.path.abspath(target_path)}...")
    combine_all_files(target_path, result_file, exclude_folders, exclude_filenames)
    print(f"Success! Old files cleared. Everything combined into: {result_file}")

# konga_debug.py  — run once, then never again
# Captures every network request Konga makes when you search
# python konga_debug.py

# from playwright.sync_api import sync_playwright
# import json, time
#
# QUERY = "charger"
# OUTPUT = "konga_network.json"
#
# captured = []
#
# with sync_playwright() as p:
#     browser = p.chromium.launch(headless=False)  # headless=False so you can watch
#     page = browser.new_page()
#
#     def on_response(resp):
#         try:
#             url = resp.url
#             status = resp.status
#             ct = resp.headers.get("content-type", "")
#             size = 0
#
#             body = None
#             if "json" in ct:
#                 try:
#                     body = resp.json()
#                     size = len(str(body))
#                 except Exception:
#                     pass
#
#             entry = {"url": url, "status": status, "content_type": ct, "size": size}
#             if body is not None:
#                 entry["body_preview"] = str(body)[:300]
#             captured.append(entry)
#
#             # Print interesting ones immediately
#             if "json" in ct and size > 500:
#                 print(f"[{status}] {size:>8} bytes  {url[:100]}")
#         except Exception:
#             pass
#
#     page.on("response", on_response)
#     page.goto(f"https://www.konga.com/search?search={QUERY}", wait_until="networkidle", timeout=60000)
#     time.sleep(5)  # let lazy loads finish
#
#     browser.close()
#
# with open(OUTPUT, "w") as f:
#     json.dump(captured, f, indent=2)
#
# print(f"\nSaved {len(captured)} requests to {OUTPUT}")
# print("\nJSON responses > 1KB:")
# for e in captured:
#     if "json" in e.get("content_type","") and e.get("size", 0) > 1000:
#         print(f"  {e['size']:>10} bytes  {e['url']}")


# konga_capture.py — run once, captures the exact API request format
# from playwright.sync_api import sync_playwright
# import json, time
#
# with sync_playwright() as p:
#     browser = p.chromium.launch(headless=False)
#     page = browser.new_page()
#
#     captured = {"requests": [], "responses": []}
#
#     def on_request(req):
#         if "igbimo" in req.url or "graphql" in req.url:
#             entry = {
#                 "url": req.url,
#                 "method": req.method,
#                 "headers": dict(req.headers),
#                 "body": req.post_data,
#             }
#             captured["requests"].append(entry)
#             print(f"\n→ REQUEST: {req.method} {req.url}")
#             print(f"  Body: {str(req.post_data)[:300]}")
#
#     def on_response(resp):
#         if "igbimo" in resp.url or ("graphql" in resp.url and resp.status == 200):
#             try:
#                 data = resp.json()
#                 entry = {"url": resp.url, "status": resp.status, "data_keys": list(data.keys()) if isinstance(data, dict) else "list"}
#                 # Find the product list
#                 def find_list(obj, depth=0):
#                     if depth > 5: return None
#                     if isinstance(obj, list) and obj and isinstance(obj[0], dict):
#                         keys = list(obj[0].keys())
#                         if any(k in keys for k in ("name","url_key","sku","slug")):
#                             return len(obj), keys[:8]
#                     if isinstance(obj, dict):
#                         for v in obj.values():
#                             r = find_list(v, depth+1)
#                             if r: return r
#                 result = find_list(data)
#                 entry["product_list_found"] = result
#                 captured["responses"].append(entry)
#                 print(f"\n← RESPONSE: {resp.url[:80]}")
#                 print(f"  Keys: {entry['data_keys']}")
#                 print(f"  Products: {result}")
#             except Exception as e:
#                 print(f"  Parse error: {e}")
#
#     page.on("request", on_request)
#     page.on("response", on_response)
#
#     page.goto("https://www.konga.com/search?search=charger", wait_until="networkidle", timeout=60000)
#     time.sleep(3)
#     browser.close()
#
# with open("konga_capture.json", "w") as f:
#     json.dump(captured, f, indent=2)
# print("\nSaved to konga_capture.json")



# import re
# from bs4 import BeautifulSoup


# def parse_html(html_content):
#     soup = BeautifulSoup(html_content, "html.parser")
#     products = []

#     # --- SCENARIO A: Extracting from the Live Search Dropdown Box ---
#     # The drop-down results container sits right below the search form
#     dropdown_container = soup.find(
#         "div", class_=lambda c: c and "absolute" in c and "max-h-95" in c
#     )

#     if dropdown_container:
#         # Find individual rows inside the search dropdown
#         items = dropdown_container.find_all(
#             "div", class_=lambda c: c and "cursor-pointer" in c
#         )
#         for item in items:
#             title_tag = item.find(
#                 "div", class_=lambda c: c and "truncate" in c
#             )
#             price_tag = item.find(
#                 "div", class_=lambda c: c and "text-red-600" in c
#             )
#             img_tag = item.find("img")

#             if title_tag and price_tag:
#                 products.append(
#                     {
#                         "source": "search_dropdown",
#                         "title": title_tag.get_text(strip=True),
#                         "price": price_tag.get_text(strip=True),
#                         "image": img_tag.get("src") if img_tag else None,
#                     }
#                 )

#     # --- SCENARIO B: Extracting from Main Shop Grid (Product Cards) ---
#     # The actual product cards on the shop page use a group flex layout wrapper
#     grid_cards = soup.find_all(
#         "div", class_=lambda c: c and "group" in c and "flex-col" in c
#     )

#     for card in grid_cards:
#         # Avoid catching elements that aren't product listings
#         link_tag = card.find("a", href=re.compile(r"^/products/"))
#         if not link_tag:
#             continue

#         img_tag = link_tag.find("img")
#         title = img_tag.get("alt") if img_tag else None

#         # Price is nested safely inside the flex column text-red section
#         price_tag = card.find("span", class_=lambda c: c and "text-red-600" in c)

#         if title and price_tag:
#             products.append(
#                 {
#                     "source": "shop_grid",
#                     "title": title.strip(),
#                     "price": price_tag.get_text(strip=True),
#                     "image": img_tag.get("src") if img_tag else None,
#                 }
#             )

#     return products


# # Test snippet execution
# if __name__ == "__main__":
#     with open("slot_debug.html", "r", encoding="utf-8") as f:
#         html_data = f.read()

#     extracted_items = parse_html(html_data)
#     print(f"Successfully extracted {len(extracted_items)} items:")
#     for idx, prod in enumerate(extracted_items, 1):
#         print(
#             f"[{prod['source'].upper()}] {idx}. {prod['title']} -> {prod['price']}"
#         )



# import re
#
# input_file = "slot_debug.html"
# output_file = "grid_container_results.txt"
# search_term = "iPhone charger"
#
# try:
#     with open(input_file, "r", encoding="utf-8", errors="ignore") as infile:
#         content = infile.read()
#
#     # 1. Regex to find script tags containing our term
#     # It captures the script tag AND the next 3000 characters of HTML to find the grid container
#     pattern = re.compile(
#         r'(<script[^>]*>[^<]*' + re.escape(search_term) + r'[^<]*</script>)([\s\S]{0,3000})',
#         re.IGNORECASE
#     )
#
#     matches = list(pattern.finditer(content))
#
#     if not matches:
#         print(f"Could not find any script blocks containing '{search_term}'.")
#     else:
#         output_data = []
#         for idx, match in enumerate(matches, 1):
#             script_tag = match.group(1)
#             surrounding_html = match.group(2)
#
#             output_data.append(f"=== MATCH #{idx} ===\n")
#             output_data.append(f"Found inside script tag (truncated preview):\n{script_tag[:300]}...\n\n")
#             output_data.append("--- NESTED / NEARBY HTML CONTAINERS DIRECTLY AFTER THIS SCRIPT ---\n")
#             output_data.append(surrounding_html)
#             output_data.append("\n\n" + "=" * 50 + "\n\n")
#
#         with open(output_file, "w", encoding="utf-8") as outfile:
#             outfile.writelines(output_data)
#
#         print(f"Success! Analyzed {len(matches)} occurrences. Results saved to '{output_file}'.")
#
# except FileNotFoundError:
#     print(f"Error: Target file '{input_file}' not found.")

# Define the absolute file path using a raw string (r"") to handle Windows backslashes
# file_path = r"C:\Users\DELL\Desktop\New Text Document.txt"
#
# # List of markers to scan for (common web application framework state objects)
# markers = [
#     "__NEXT_DATA__",
#     "__NUXT__",
#     "__INITIAL_STATE__",
#     "__PRELOADED_STATE__"
# ]
#
# try:
#     # Open the file securely with UTF-8 encoding and ignore decoding errors if the file has weird characters
#     with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
#         file_content = file.read()
#
#     # Iterate and search for each marker inside the file content
#     for marker in markers:
#         if marker in file_content:
#             print(marker)
#
# except FileNotFoundError:
#     print(f"Error: The file at '{file_path}' was not found. Please check the path.")
# except Exception as e:
#     print(f"An unexpected error occurred: {e}")


# import json
#
# file_path = r"C:\Users\DELL\Desktop\New Text Document.txt"
# output_json_path = r"C:\Users\DELL\Desktop\New Text Document_parsed.json"
#
# try:
#     with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
#         content = file.read()
#
#     # Find where the JSON starts
#     start_idx = content.find('{"props"')
#
#     if start_idx != -1:
#         # Extract everything from {"props" to the very end of the file
#         json_snippet = content[start_idx:]
#
#         # Use raw decoder to parse until the valid JSON structure ends, ignoring trailing data
#         decoder = json.JSONDecoder()
#         parsed_json, index = decoder.raw_decode(json_snippet)
#
#         # Save the clean, formatted JSON to your desktop
#         with open(output_json_path, "w", encoding="utf-8") as out_file:
#             json.dump(parsed_json, out_file, indent=4, ensure_ascii=False)
#
#         print(f"Success! Cleaned JSON extracted securely to: {output_json_path}")
#         print(f"Parsed {index} characters of pure data.")
#
#     else:
#         print("Error: Could not find the beginning of the Next.js data structure ('{\"props\"').")
#
# except Exception as e:
#     print(f"An unexpected error occurred: {e}")
