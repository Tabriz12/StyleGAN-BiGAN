from PIL import Image
import os

print("shrinking images in folder")

folder = input("folder path: ")
w = int(input("width: "))
h = int(input("height: "))

for file in os.listdir(folder):
    new_file = f"{folder}\\{file}"
    im = Image.open(new_file)
    im = im.resize((w, h), Image.ANTIALIAS)
    im.save(new_file)

print("Done")
