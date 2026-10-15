from i3ipc import Connection
import json
import argparse
import sys
import shlex
import re
from pathlib import Path

def getXwaylandClass(leaf):
    windowProperties = leaf.ipc_data.get("window_properties")
    # Not all leaves have windowProperties (Only xwayland leaves)
    if(windowProperties):
        return windowProperties.get("class")
    else:
        return None

def getWaylandAppId(leaf):
    # All leaves have app_id
    return leaf.app_id

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
    fp = open(configPath, "r")
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

    # Exit if this is an i3 session
    if (i3.get_version().ipc_data.get("variant") != 'sway'):
        i3.command(
            f"exec --no-startup-id i3-nag -t warning -m 'i3-window-switcher is not actually written for i3! (Its for sway atm)')"
        )
        sys.exit(1)


    getClass = getWaylandAppId # Start by looking for App_id
    currentWindow = tree.find_focused()
    existingWindows = [];
    for leaf in tree.leaves():
        leafClass = getClass(leaf)
        # "leafClass and" prevents an error if leafClass is null
        if leafClass and re.fullmatch(windowClass, getClass(leaf)):
            existingWindows.append(leaf);

    # If nothing can be found via App_id check for an Xwayland class
    if(not existingWindows):
        getClass = getXwaylandClass # Change get function to Xwayland variety
        for leaf in tree.leaves():
            leafClass = getClass(leaf)
            if leafClass and re.fullmatch(windowClass, getClass(leaf)):
                existingWindows.append(leaf);

    focusNext = False 
    if (len(existingWindows) > 0):
        if (getClass(currentWindow) == windowClass):
            for window in existingWindows:
                if (currentWindow == window):
                    focusNext = True;
                    continue
                if (focusNext):
                    window.command("focus child")
                    break
        else: # currentWindow.window_class != windowClass
            existingWindows[0].command("focus child")
    
    else: # currentWindow !exist
        reply = i3.command(f"exec --no-startup-id {startCommand}")

if __name__ == "__main__":
    main()