"""Fictional reference data: acquirers, issuers, payment methods, merchants and cities.

Every bank, PSP, wallet and merchant name here is invented. City names are real places, which
is fine: they are geography, not institutions. Card BINs generated from this module all start
with ``9`` (an ISO/IEC 7812 range not used by the major card networks), so they cannot be
mistaken for real issuer BINs.
"""

from __future__ import annotations

from typing import Final

REGIONS: Final = ("north", "south", "east", "west")

CITIES: Final[dict[str, tuple[str, ...]]] = {
    "north": ("Delhi", "Gurugram", "Noida", "Chandigarh", "Jaipur", "Lucknow"),
    "south": ("Bengaluru", "Chennai", "Hyderabad", "Kochi", "Coimbatore"),
    "east": ("Kolkata", "Bhubaneswar", "Patna", "Guwahati", "Ranchi"),
    "west": ("Mumbai", "Pune", "Ahmedabad", "Surat", "Panaji"),
}

# (acquirer_id, acquirer_name)
ACQUIRERS: Final = (
    ("ACQ_KESTREL", "Kestrel Bank"),
    ("ACQ_MERIDIAN", "Meridian Bank"),
    ("ACQ_SAFFRON", "Saffron Bank"),
    ("ACQ_NORTHSTAR", "Northstar Bank"),
)

# (issuer_id, issuer_name, issuer_type)
ISSUERS: Final = (
    ("BNK_HARBOR", "Harbor Bank", "bank"),
    ("BNK_LOTUS", "Lotus Bank", "bank"),
    ("BNK_RIDGE", "Ridge Bank", "bank"),
    ("BNK_INDIGO", "Indigo Bank", "bank"),
    ("BNK_CORAL", "Coral Bank", "bank"),
    ("BNK_BANYAN", "Banyan Bank", "bank"),
    ("PSP_ZEPHYR", "Zephyr", "upi_psp"),
    ("PSP_PAYNEST", "PayNest", "upi_psp"),
    ("PSP_TEZRA", "Tezra", "upi_psp"),
    ("PSP_ORCHID", "Orchid", "upi_psp"),
    ("WAL_POCKETLY", "Pocketly", "wallet"),
    ("WAL_WALLETRA", "Walletra", "wallet"),
)

# (payment_method, method_family)
PAYMENT_METHODS: Final = (
    ("UPI", "upi"),
    ("CREDIT_CARD", "card"),
    ("DEBIT_CARD", "card"),
    ("CARD_EMI", "card"),
    ("NETBANKING", "netbanking"),
    ("WALLET", "wallet"),
)

CATEGORIES: Final = (
    "grocery",
    "electronics",
    "fashion",
    "travel",
    "fuel",
    "quick_commerce",
    "pharmacy",
    "dining",
)

TIERS: Final = ("enterprise", "mid_market", "smb")

MERCHANT_PREFIXES: Final = (
    "Amber",
    "Cedar",
    "Maple",
    "Silverline",
    "Bluebell",
    "Goldleaf",
    "Urbanly",
    "Crescent",
    "Jasmine",
    "Peacock",
    "Monsoon",
    "Tamarind",
    "Neem Tree",
    "Sandalwood",
    "Vistara Lane",
    "Prism",
    "Aurora",
    "Nimbus",
    "Granite",
    "Willow",
    "Sapphire",
    "Ember",
    "Horizonline",
    "Pebble",
    "Quartz",
    "Rustic",
    "Summit",
    "Tulip",
    "Velvet",
    "Zenara",
)

MERCHANT_SUFFIXES: Final[dict[str, tuple[str, ...]]] = {
    "grocery": ("Grocers", "Fresh Mart", "Provisions", "Daily Needs"),
    "electronics": ("Electronics", "Digital", "Gadget House", "Tech Store"),
    "fashion": ("Fashions", "Apparel", "Boutique", "Threads"),
    "travel": ("Travels", "Holidays", "Journeys", "Trips"),
    "fuel": ("Fuels", "Petro Point", "Fuel Station", "Energy"),
    "quick_commerce": ("Express", "Dash", "QuickCart", "Instant"),
    "pharmacy": ("Pharmacy", "Chemists", "Health Store", "Wellness"),
    "dining": ("Kitchen", "Cafe", "Bistro", "Eatery"),
}

# Named merchant groups. NovaMart is the S2 (hero) group; the others are background chains.
NOVAMART_GROUP: Final = "NovaMart"
NOVAMART_MERCHANTS: Final = (
    # (merchant_name, category, region, city)
    ("NovaMart Electronics", "electronics", "west", "Mumbai"),
    ("NovaMart Fashion", "fashion", "north", "Gurugram"),
    ("NovaMart Grocery", "grocery", "south", "Bengaluru"),
)
BACKGROUND_GROUPS: Final = (
    # (group_name, category, number of merchants)
    ("StellarFuel", "fuel", 6),
    ("Mosaic Cafe", "dining", 5),
)

# The S7 merchant with an unexplained settlement shortfall.
ORBIT_MERCHANT: Final = ("Orbit Electronics", "electronics", "west", "Pune")
