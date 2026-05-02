import os

def fix_encoding(file_path):
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
            
        # Try different encodings
        decoded_content = None
        for enc in ['utf-8-sig', 'utf-8', 'cp949', 'euc-kr']:
            try:
                decoded_content = content.decode(enc)
                used_enc = enc
                break
            except UnicodeDecodeError:
                continue
        
        if decoded_content is not None:
            # Check for mangled characters like '?' where they shouldn't be
            # This is hard to automate perfectly, but we can at least normalize to UTF-8
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(decoded_content)
            print(f"Converted {file_path} from {used_enc} to utf-8")
        else:
            print(f"Could not decode {file_path}")
            
    except Exception as e:
        print(f"Error processing {file_path}: {e}")

if __name__ == "__main__":
    for root, dirs, files in os.walk('.'):
        if any(x in root for x in ['venv', '.git', '__pycache__', 'cache', 'logs']):
            continue
        for file in files:
            if file.endswith('.py'):
                fix_encoding(os.path.join(root, file))
