import requests
import time
import sqlite3
from flask import redirect, render_template, session
from functools import wraps
from functools import lru_cache



## Cache configuration
CACHE_DURATION = 300  # 5 minutes cache duration
COINGECKO_CACHE_DURATION = 3600  # 1 hour for coingecko data
price_cache = {}
coins_cache = {
    'data': None,
    'timestamp': 0
}



def apology(message, code=400):
    """Render message as an apology to user."""

    def escape(s):
        """
        Escape special characters.

        https://github.com/jacebrowning/memegen#special-characters
        """
        for old, new in [
            ("-", "--"),
            (" ", "-"),
            ("_", "__"),
            ("?", "~q"),
            ("%", "~p"),
            ("#", "~h"),
            ("/", "~s"),
            ('"', "''"),
        ]:
            s = s.replace(old, new)
        return s

    return render_template("apology.html", top=code, bottom=escape(message)), code


def login_required(f):
    """
    Decorate routes to require login.

    https://flask.palletsprojects.com/en/latest/patterns/viewdecorators/
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated_function


def get_market_cap():
    """Get global cryptocurrency market cap"""
    try:
        url = "https://api.coingecko.com/api/v3/global"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        # Format market cap to a readable string
        market_cap = data['data']['total_market_cap']['usd']
        if market_cap >= 1_000_000_000_000:  # Trillion
            return f"{market_cap / 1_000_000_000_000:.2f}T"
        elif market_cap >= 1_000_000_000:  # Billion
            return f"{market_cap / 1_000_000_000:.2f}B"
        else:  # Million
            return f"{market_cap / 1_000_000:.2f}M"

    except requests.RequestException as e:
        print(f"Error fetching market cap: {e}")
        return "N/A"

def fetch_top_coins():
    """Fetch top coins with caching to prevent rate limiting"""
    current_time = time.time()

    # Return cached data if it's still valid
    if coins_cache['data'] is not None and current_time - coins_cache['timestamp'] < CACHE_DURATION:
        return coins_cache['data']

    try:
        # If cache is expired but exists, add a small delay before fetching new data
        if coins_cache['data'] is not None:
            time.sleep(1)

        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 200,
            "page": 1,
            "sparkline": False,
            "price_change_percentage": "1h,24h,7d"
        }

        response = requests.get(url, params=params)
        if response.status_code == 429:
            print("Rate limit hit, using cached data if available")
            return coins_cache['data'] if coins_cache['data'] is not None else []

        response.raise_for_status()
        coins = response.json()

        # Format coins data
        formatted_coins = []
        for coin in coins:
            formatted_coin = {
                "id": coin['id'],
                "name": coin['name'],
                "symbol": coin['symbol'].upper(),
                "current_price": coin['current_price'],
                "market_cap": coin['market_cap'],
                "market_cap_rank": coin['market_cap_rank'],
                "image_url": coin['image'],
                "price_change_percentage_1h": coin.get('price_change_percentage_1h_in_currency', 0),
                "price_change_percentage_24h": coin.get('price_change_percentage_24h_in_currency', 0),
                "price_change_percentage_7d": coin.get('price_change_percentage_7d_in_currency', 0)
            }
            formatted_coins.append(formatted_coin)

        # Update cache
        coins_cache['data'] = formatted_coins
        coins_cache['timestamp'] = current_time

        return formatted_coins

    except requests.RequestException as e:
        print(f"Error fetching coins: {e}")
        # Return cached data if available, empty list if not
        if coins_cache['data'] is not None:
            print("Returning cached data due to error")
            return coins_cache['data']
        return []


# Add to your existing cache configuration in helpers.py
single_coin_cache = {}

# Function to connect to SQLite database
def get_db_connection():
    conn = sqlite3.connect('finance.db')
    conn.row_factory = sqlite3.Row  # To Access columns by name
    return conn

# Function to fetch data for a single coin by name
def fetch_single_coin(coin_id):
    """Fetch a single coin by its ID from CoinGecko API with caching."""
    current_time = time.time()

    # Check cache first
    if coin_id in single_coin_cache:
        cached_data = single_coin_cache[coin_id]
        if current_time - cached_data['timestamp'] < CACHE_DURATION:
            return cached_data['data']

    try:
        # Only make one API call for price data
        price_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24h_change=true"
        price_response = requests.get(price_url)

        # Return cached data if rate limit hit
        if price_response.status_code == 429 and coin_id in single_coin_cache:
            print(f"Rate limit hit for {coin_id}, using cached data")
            return single_coin_cache[coin_id]['data']

        price_response.raise_for_status()
        price_data = price_response.json()

        if coin_id not in price_data:
            return None

        # Get coin details from database instead of API
        db = get_db_connection()
        coin_data = db.execute("""
            SELECT name, symbol, image_url
            FROM coins
            WHERE coin_id = ?
        """, (coin_id,)).fetchone()
        db.close()

        if not coin_data:
            return None

        result = {
            "id": coin_id,
            "name": coin_data["name"],
            "symbol": coin_data["symbol"].upper(),
            "price": price_data[coin_id]["usd"],
            "change_24h": price_data[coin_id].get("usd_24h_change", 0),
            "image_url": coin_data["image_url"]
        }

        # Cache the result
        single_coin_cache[coin_id] = {
            'data': result,
            'timestamp': current_time
        }

        return result

    except requests.RequestException as e:
        print(f"Error fetching coin {coin_id}: {e}")
        # Return cached data if available
        if coin_id in single_coin_cache:
            print(f"Returning cached data for {coin_id}")
            return single_coin_cache[coin_id]['data']
        return None



def lookup(coin_id):
    """Look up cryptocurrency information using cached data."""
    try:
        # First try to get data from cached top coins
        cached_coins = fetch_top_coins()
        coin_data = next((coin for coin in cached_coins if coin['id'] == coin_id), None)

        if coin_data:
            return {
                "id": coin_id,
                "name": coin_data["name"],
                "symbol": coin_data["symbol"],
                "price": coin_data["current_price"],
                "change_24h": coin_data.get("price_change_percentage_24h", 0),
                "image_url": coin_data["image_url"]
            }
    except Exception as e:
        print(f"Error looking up {coin_id}: {str(e)}")
        return None

    return None


# Add a function to clear expired cache entries
def clear_expired_cache():
    current_time = time.time()
    expired_keys = [k for k, (timestamp, _) in price_cache.items()
                   if current_time - timestamp > CACHE_DURATION]
    for k in expired_keys:
        price_cache.pop(k)


def usd(value):
    """Format value as USD."""
    return f"${value:,.2f}"


def get_supported_cryptocurrencies():
    """Return a list of supported cryptocurrency names."""
    return sorted(set(COIN_GECKO_IDS.keys()))


def format_crypto_price(value):
    """
    Format cryptocurrency prices and values to display appropriate decimal places:
    - For values >= 1: Show up to 2 decimal places (e.g., "21.01", "21")
    - For values < 1: Show up to 8 decimal places without trailing zeros
    - Adds dollar sign

    Examples:
    21.01234 -> $21.01
    0.00000421 -> $0.00000421
    1.10000000 -> $1.1
    0.00100000 -> $0.001
    """
    try:
        value = float(value)
        if value == 0:
            return "$0"

        if value >= 1:
            # For values >= 1, show up to 2 decimal places
            formatted = f"${value:.2f}".rstrip('0').rstrip('.')
            return formatted if '.' in formatted else f"{formatted}"
        else:
            # For values < 1, show up to 8 decimal places
            formatted = f"${value:.8f}".rstrip('0').rstrip('.')
            return formatted

    except (ValueError, TypeError):
        return "$0"

def format_crypto_quantity(value):
    """
    Format cryptocurrency quantities:
    - For values >= 1: Show up to 2 decimal places (e.g., "21.01", "21")
    - For values < 1: Show up to 8 decimal places without trailing zeros

    Examples:
    21.01234 -> 21.01
    0.00000421 -> 0.00000421
    1.10000000 -> 1.1
    0.00100000 -> 0.001
    """
    try:
        value = float(value)
        if value == 0:
            return "0"

        if value >= 1:
            # For values >= 1, show up to 2 decimal places
            return f"{value:.2f}".rstrip('0').rstrip('.')
        else:
            # For values < 1, show up to 8 decimal places
            return f"{value:.8f}".rstrip('0').rstrip('.')

    except (ValueError, TypeError):
        return "0"


def format_percentage(value):
    """Format percentage values to 2 decimal places"""
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return "0.00"
