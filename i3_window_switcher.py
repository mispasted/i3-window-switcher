from i3ipc import Connection
import json
import argparse
import sys
import shlex
from pathlib import Path


def main():
    defaultConfigPath = str(Path.home() / ".config" / "i3-window-switcher" / "config.json")

    # Parse the argument.
    parser = argparse.ArgumentParser(
        description="Open or focus a program in i3/sway"
    )
    parser.add_argument(
        "program",
        help = "Program to launch, as defined in config.json"
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Specify a path to the configuration file. Default path is: " + defaultConfigPath
    )
    args = parser.parse_args()
    program = args.program

    # Check if a alternate config file location was passed
    configPath = args.config if args.config is not None else defaultConfigPath

    # Open the Configuration
    fp = open("./config.json", "r")
    jsonData = json.load(fp)
    
    # ensure the desired command is configured
    programDefined = False
    for key in jsonData:
        if key == program:
            programDefined = True
            break
            
    if not programDefined:
        print("Error: '" + program + "' not defined in configuration file.", file=sys.stderr)
        sys.exit(1)
    
    # Set definitions
    windowClass = jsonData[program]["class"] 
    commandArgs = jsonData[program]["command"] 
    startCommand = shlex.join(commandArgs)
    
    i3 = Connection()
    tree = i3.get_tree()
    
    currentWindow = tree.find_focused()
    windowsOfClass = tree.find_classed(windowClass)
    
    if (len(windowsOfClass) > 0):
        if (currentWindow.window_class == windowClass):
            for i in range(0, len(windowsOfClass)):
                if (currentWindow == windowsOfClass[i]):
    
                    # focus next window in tree
                    if (i == len(windowsOfClass) - 1):
                        index = 0
                    else:
                        index = i + 1
                    windowsOfClass[index].command("focus")
    
                    break
        else: # currentWindow.window_class != windowClass
            windowsOfClass[0].command("focus")
    
    else: # currentWindow !exist
        reply = i3.command(f"exec --no-startup-id {startCommand}")

if __name__ == "__main__":
    main()