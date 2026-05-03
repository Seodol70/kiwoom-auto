
import sys

def check_quotes(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    pos = -1
    quotes = []
    while True:
        pos = content.find('"""', pos + 1)
        if pos == -1: break
        quotes.append(pos)
    
    print(f"Total triple quotes: {len(quotes)}")
    
    for i, p in enumerate(quotes):
        line_no = content.count('\n', 0, p) + 1
        print(f"Quote {i+1} at line {line_no}")

if __name__ == "__main__":
    check_quotes(sys.argv[1])
