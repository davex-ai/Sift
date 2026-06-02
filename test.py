import requests

payload = { 'api_key': '9eddd207a3ed04c58af623ff04302d75', 'url': 'https://www.jumia.com.ng/mlp-best-home-appliances-deals/blenders-and-mixers/', 'output_format': 'text' }
r = requests.get('https://api.scraperapi.com/', params=payload)
print(r.text)


