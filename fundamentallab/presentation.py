import json
def parse_criteria_json(text):
    try:return json.loads(text or '[]')
    except:return []
