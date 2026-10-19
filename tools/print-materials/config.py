"""What the printed materials say. Edit, then run: python make.py"""

NAME = "Edna's Piso Wifi"          # the name on the portal (dashboard: Settings > Name and look)
SSID = "Edna's Piso Wifi"          # the Wi-Fi network name customers join
ADDRESS = "http://piso.wifi"       # where customers check their time
WINDOW_SECONDS = 60                # Insert Coin time limit

MINUTES_PER_PESO = 10              # base rate
PROMOS = [(5, 60), (10, 180), (15, 360), (20, 600), (25, 900), (30, 1440),
          (40, 2880), (50, 4320), (100, 10080), (400, 44640)]   # (pesos, minutes)
BEST_VALUE = 400                   # which amount gets the "BEST VALUE" tag (None for none)
