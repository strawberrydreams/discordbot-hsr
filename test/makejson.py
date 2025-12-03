import json

# 기존 txt 파일 읽기
with open('../settings/prohibited_words.txt', 'r', encoding='utf-8') as f:
    words = [line.strip() for line in f if line.strip()]

# json 파일로 저장
with open('../settings/prohibited_words.json', 'w', encoding='utf-8') as f:
    json.dump(words, f, ensure_ascii=False, indent=2)

# 기본적으로 prohibition_words.txt 파일을 .json으로 변환하는 프로그램임
# 다른 파일을 원한다면 같은 디렉토리에 넣어놓고 이 코드에서 파일명만 바꾸면 됨