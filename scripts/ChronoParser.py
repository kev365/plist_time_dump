#Iterate through folder and look for plist files
import os
import mmap

path = "E:\iPhoneTestData\CeoJoe_iPhone"
bplist = {"format": "plist", "offset": 0, "signature": ["62 70 6C 69 73 74 30 30", "62 70 6C 69 73 74 30 00"]}

#data = json.loads(open(os.path.join(path, "data.json"), "r", encoding="utf-8").read())["data"]

#listing plist files with scandir (this is not looking at subdiectories)
'''
with os.scandir(path) as listOfEntries:
    for entry in listOfEntries:
        # print all entries that are files
        if entry.is_file():
            print(entry.name)
'''
'''
def get_plist_files(path):
    for entry in os.scandir(path):
        if entry.is_file and entry.name.endswith(".plist"):
            yield os.path.join(path, entry.name)
        #else:
        #    yield get_plist_files(entry.path)

for file_match in get_plist_files(path):
    print(file_match)    
'''
#Listing Plist files with os walk
def get_plist_files(path):
    print("Finding Plists based on Extension...")
    for root, dirs, files in os.walk(path):
        for file in files:
            if file.endswith(".plist"):
                 print(os.path.join(root, file))

def get_plist_files_via_signature(path):
    print("Finding Plists based on file signature...")
    for root, dirs, files in os.walk(path):

        for file in files:
            read_header = open(os.path.join(root, file), "rb").read(8)
            #print(read_header)
            if read_header.__contains__('bplist00')
                print(os.path.join(root, file))



#get_plist_files(path)
get_plist_files_via_signature(path)