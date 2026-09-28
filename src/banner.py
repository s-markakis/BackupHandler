from colorama import Fore, Style, init

# Initialize colorama
init(autoreset=True)


def print_banner():
    banner = f"""{Fore.CYAN}{Style.BRIGHT}
    ██████╗  █████╗  ██████╗██╗  ██╗██╗   ██╗██████╗
    ██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██║   ██║██╔══██╗
    ██████╔╝███████║██║     █████╔╝ ██║   ██║██████╔╝
    ██╔══██╗██╔══██║██║     ██╔═██╗ ██║   ██║██╔═══╝
    ██████╔╝██║  ██║╚██████╗██║  ██╗╚██████╔╝██║
    ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝

            ██╗  ██╗ █████╗ ███╗   ██╗██████╗ ██╗     ███████╗██████╗
            ██║  ██║██╔══██╗████╗  ██║██╔══██╗██║     ██╔════╝██╔══██╗
            ███████║███████║██╔██╗ ██║██║  ██║██║     █████╗  ██████╔╝
            ██╔══██║██╔══██║██║╚██╗██║██║  ██║██║     ██╔══╝  ██╔══██╗
            ██║  ██║██║  ██║██║ ╚████║██████╔╝███████╗███████╗██║  ██║
            ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝
    {Style.RESET_ALL}"""

    creator_info = f"""{Fore.GREEN}{Style.BRIGHT}
    ┌────────────────────────────────────────────────────────────────┐
    │                  Created with ❤️ by SP1R4-R                     │
    │              Backup Handler - Your Data, Secured               │
    │                                                                │
    │  Backup Handler is a powerful and flexible backup solution     │
    │  that supports local and remote backups, including SSH.        │
    │  It offers full, incremental, and differential backup modes,   │
    │  along with scheduling capabilities and Telegram notifications.│
    │  Secure your data with ease using Backup Handler!              │
    └────────────────────────────────────────────────────────────────┘
    {Style.RESET_ALL}"""

    social_links = f"""{Fore.YELLOW}{Style.BRIGHT}
    ┌───────────────────────────────────────────────────────────────┐
    │                        Social Links                           │
    ├───────────────────────────────────────────────────────────────┤
    │  Find me here: https://sp1r4.github.io                        │
    └───────────────────────────────────────────────────────────────┘
    {Style.RESET_ALL}"""

    print(banner)
    print(creator_info)
    print(social_links)
