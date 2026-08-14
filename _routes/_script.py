import os

for datei in os.listdir():
    with open(datei) as file:
        text = file.read()
    if len(text) < 10:
        os.remove(datei)