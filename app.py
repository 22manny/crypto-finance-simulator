import sqlite3
import markdown
import os
import random
from datetime import datetime
from flask import Flask, flash, redirect, render_template, request, session, url_for, jsonify
from flask_session import Session
from werkzeug.security import check_password_hash, generate_password_hash
from functools import lru_cache
import time


from helpers import get_market_cap, apology, login_required, lookup, usd, fetch_top_coins, fetch_single_coin
from helpers import format_crypto_quantity, format_crypto_price, format_percentage

# Configure application
app = Flask(__name__)
app.secret_key = 'manny_key'  # Replace with a real secret key
app.config['UPLOAD_FOLDER'] = 'static/uploads'

# Custom filter
app.jinja_env.filters["usd"] = usd

# Add this near the top of app.py where you configure your Flask app
app.jinja_env.filters['format_crypto_price'] = format_crypto_price


# In app.py
app.jinja_env.filters['format_crypto_quantity'] = format_crypto_quantity

app.jinja_env.filters['format_percentage'] = format_percentage

def format_crypto_amount(value):
    """Custom filter to format cryptocurrency amounts"""
    try:
        value = float(value)
        if value >= 1:
            return '{:.2f}'.format(value)
        else:
            # Convert to string and remove trailing zeros
            s = '{:.8f}'.format(value)
            while s.endswith('0'):
                s = s[:-1]
            if s.endswith('.'):
                s = s[:-1]
            return s
    except (ValueError, TypeError):
        return value

# Register the custom filter
app.jinja_env.filters['format_crypto'] = format_crypto_amount


# Configure session to use filesystem (instead of signed cookies)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

# Function to connect to SQLite database
def get_db_connection():
    conn = sqlite3.connect('finance.db')
    conn.row_factory = sqlite3.Row  # To Access columns by name
    return conn

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow}


@app.after_request
def after_request(response):
    """Ensure responses aren't cached"""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Expires"] = 0
    response.headers["Pragma"] = "no-cache"
    return response


# Security questions list
SECURITY_QUESTIONS = [
    "What was the name of your first pet?",
    "What is the name of the town where you were born?",
    "What is your favorite book?",
    "What is your mother's maiden name?",
    "What is the make of your first car?",
    "What is the name of your favorite teacher?",
    "What is your childhood best friend's name?"
]


# Constant for fee calculation all transactions
TRANSACTION_FEE_RATE = 0.0015  # 0.15% fee (similar to Binance's basic fee)


# Cache trading pairs data for 5 minutes
@lru_cache(maxsize=128)
def get_cached_trading_pairs():
    return fetch_top_coins()


@app.route("/api/trading_pairs/<coin_id>")
@login_required
def get_trading_pairs(coin_id):
    try:
        # Use cached data
        all_coins = get_cached_trading_pairs()
        # Filter out the source coin and format the response
        trading_pairs = [
            {
                'id': coin['id'],
                'name': coin['name'],
                'symbol': coin['symbol'],
                'price': coin['current_price']
            }
            for coin in all_coins
            if coin['id'] != coin_id
        ]
        return jsonify(trading_pairs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route("/api/price/<coin_id>")
@login_required
def get_coin_price(coin_id):  # Changed from get_price to get_coin_price
    try:
        # Use cached data
        all_coins = get_cached_trading_pairs()
        coin_data = next((coin for coin in all_coins if coin['id'] == coin_id), None)

        if coin_data:
            return jsonify({"price": coin_data['current_price']})
        return jsonify({"error": "Price not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        try:
            # Non-logged in view remains the same
            coins = fetch_top_coins()
            market_cap = get_market_cap()

            if not coins:
                raise Exception("No coins data available")

            return render_template("index.html",
                                 logged_in=False,
                                 coins=coins,
                                 market_cap=market_cap,
                                 error=None)
        except Exception as e:
            print(f"Error in index route: {str(e)}")
            return render_template("index.html",
                                 logged_in=False,
                                 coins=[],
                                 market_cap="N/A",
                                 error="Unable to fetch cryptocurrency data")
    else:
        user_id = session["user_id"]
        conn = get_db_connection()

        try:
            # Fetch user information
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                conn.close()
                return apology("User not found.")

            # Get current prices for all top coins in one call (for efficiency)
            all_coins = fetch_top_coins()
            price_map = {coin['id']: coin['current_price'] for coin in all_coins}

            # Fetch user holdings with coin information from database
            holdings = conn.execute("""
                SELECT h.crypto_name, h.quantity, h.coingecko_id,
                       c.symbol, c.image_url, c.name as full_name
                FROM holdings h
                JOIN coins c ON h.coingecko_id = c.coin_id
                WHERE h.user_id = ? AND h.quantity > 0
            """, (user_id,)).fetchall()

            # Calculate holdings' total value
            holdings_data = []
            total_value = 0

            for holding in holdings:
                quantity = float(holding['quantity'])
                coin_id = holding['coingecko_id']

                # Try to get price from top coins cache first
                if coin_id in price_map:
                    price = price_map[coin_id]
                else:
                    # For coins not in top 200, fetch individually
                    coin_data = fetch_single_coin(coin_id)
                    price = coin_data['price'] if coin_data else 0

                if price > 0:
                    value = quantity * price
                    holdings_data.append({
                        "crypto_name": holding['crypto_name'],
                        "quantity": quantity,
                        "price": price,
                        "value": value,
                        "image_url": holding['image_url'],
                        "symbol": holding['symbol'],
                        "full_name": holding['full_name'],
                        "coingecko_id": coin_id
                    })
                    total_value += value

            # Sort holdings by value (highest to lowest)
            holdings_data = sorted(holdings_data, key=lambda x: x['value'], reverse=True)

            cash_balance = user["cash"] if user["cash"] is not None else 0
            grand_total = total_value + cash_balance

            return render_template(
                "index.html",
                logged_in=True,
                holdings=holdings_data,
                cash_balance=format_crypto_price(cash_balance),
                total_value=format_crypto_price(total_value),
                grand_total=format_crypto_price(grand_total)
            )

        except Exception as e:
            return apology(f"An error occurred: {str(e)}")
        finally:
            conn.close()


@app.route("/api/price/<coin_id>")
@login_required
def get_price(coin_id):
    try:
        crypto_data = lookup(coin_id)
        if crypto_data:
            return jsonify({"price": crypto_data["price"]})
        return jsonify({"error": "Price not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/buy", methods=["GET", "POST"])
@login_required
def buy():
    if request.method == "GET":
        try:
            # Fetch top coins
            coins = fetch_top_coins()

            # Get user's cash balance
            conn = get_db_connection()
            user_cash = conn.execute("SELECT cash FROM users WHERE id = ?",
                                   (session["user_id"],)).fetchone()["cash"]
            conn.close()

            return render_template("buy.html", coins=coins, user_cash=user_cash)

        except Exception as e:
            return apology(f"Error loading cryptocurrency data: {str(e)}")

    else:  # POST request
        crypto_id = request.form.get("crypto_id")
        buy_type = request.form.get("buyType")

        # Get the amount based on buy type
        if buy_type == "dollars":
            dollars = request.form.get("dollars")
            try:
                dollars = float(dollars)
                if dollars <= 0:
                    return apology("Amount must be positive")
            except ValueError:
                return apology("Invalid dollar amount")

            # Get crypto info
            crypto_data = lookup(crypto_id)
            if not crypto_data:
                return apology("Error fetching cryptocurrency data")

            # Calculate amount of coins
            amount = dollars / crypto_data["price"]
            total_cost = dollars

        else:  # amount type
            amount = request.form.get("amount")
            try:
                amount = float(amount)
                if amount <= 0:
                    return apology("Amount must be positive")
            except ValueError:
                return apology("Invalid amount")

            # Get crypto info
            crypto_data = lookup(crypto_id)
            if not crypto_data:
                return apology("Error fetching cryptocurrency data")

            # Calculate total cost
            total_cost = amount * crypto_data["price"]

        # Check user's cash balance
        conn = get_db_connection()
        user_cash = conn.execute("SELECT cash FROM users WHERE id = ?",
                               (session["user_id"],)).fetchone()["cash"]
        conn.close()

        if total_cost > user_cash:
            return apology(f"Cannot afford! Cost (${total_cost:.2f}) exceeds your balance (${user_cash:.2f})")

        # Store all relevant data in session
        session["pending_purchase"] = {
            "crypto_id": crypto_id,
            "crypto_name": crypto_data["name"],
            "amount": amount,
            "price": crypto_data["price"],
            "total_cost": total_cost,
            "user_cash": user_cash,
            "image_url": crypto_data["image_url"]  # Add image URL here
        }

        return render_template("confirm_purchase.html")


@app.route("/confirm_purchase", methods=["POST"])
@login_required
def confirm_purchase():
    # Get the pending purchase from session
    purchase = session.get("pending_purchase")
    if not purchase:
        return apology("No pending purchase")

    try:
        conn = get_db_connection()
        user_id = session["user_id"]

        # Calculate transaction fee
        transaction_fee = purchase["total_cost"] * TRANSACTION_FEE_RATE
        total_with_fee = purchase["total_cost"] + transaction_fee

        # Check if user has enough funds including fee
        current_cash = conn.execute("SELECT cash FROM users WHERE id = ?",
                                  (user_id,)).fetchone()["cash"]

        if current_cash < total_with_fee:
            session.pop("pending_purchase", None)
            conn.close()
            return apology(f"Cannot afford! Total cost with fee (${total_with_fee:.2f}) exceeds your balance (${current_cash:.2f})")

        # Begin transaction
        conn.execute("BEGIN TRANSACTION")

        # Update user's cash balance (including fee)
        conn.execute("""
            UPDATE users
            SET cash = cash - ?
            WHERE id = ?""",
            (total_with_fee, user_id))

        # Record the transaction (now including fee)
        conn.execute("""
            INSERT INTO transactions
            (user_id, crypto_name, quantity, price, fee, type, coingecko_id)
            VALUES (?, ?, ?, ?, ?, 'buy', ?)""",
            (user_id, purchase["crypto_name"], purchase["amount"],
             purchase["price"], transaction_fee, purchase["crypto_id"]))

        # Update holdings
        existing_holding = conn.execute("""
            SELECT quantity
            FROM holdings
            WHERE user_id = ? AND crypto_name = ?""",
            (user_id, purchase["crypto_name"])).fetchone()

        if existing_holding:
            conn.execute("""
                UPDATE holdings
                SET quantity = quantity + ?
                WHERE user_id = ? AND crypto_name = ?""",
                (purchase["amount"], user_id, purchase["crypto_name"]))
        else:
            conn.execute("""
                INSERT INTO holdings (user_id, crypto_name, quantity, coingecko_id)
                VALUES (?, ?, ?, ?)""",
                (user_id, purchase["crypto_name"], purchase["amount"], purchase["crypto_id"]))

        conn.commit()
        session.pop("pending_purchase", None)

        flash(f"Successfully purchased {format_crypto_quantity(purchase['amount'])} {purchase['crypto_name']} for {format_crypto_price(purchase['total_cost'])} (Fee: {format_crypto_price(transaction_fee)})")

        conn.close()
        return redirect("/")

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return apology(f"Purchase failed: {str(e)}")



@app.route("/history")
@login_required
def history():
    conn = get_db_connection()
    user_id = session["user_id"]

    # Fetch all transactions with trade details
    transactions = conn.execute("""
        SELECT
            id,
            crypto_name,
            quantity,
            price,
            fee,
            type,
            trade_to_crypto,
            trade_to_quantity,
            timestamp
        FROM transactions
        WHERE user_id = ?
        ORDER BY timestamp DESC""",
        (user_id,)).fetchall()

    conn.close()

    return render_template("history.html", transactions=transactions)



@app.route("/get_cryptocurrencies")
@login_required
def get_cryptocurrencies():
    cryptos = get_all_cryptocurrencies()
    return jsonify(cryptos)


@app.route("/trade", methods=["GET", "POST"])
@login_required
def trade():
    conn = get_db_connection()
    user_id = session["user_id"]

    # Fetch user's holdings - Added ORDER BY for alphabetical sorting
    holdings = conn.execute("""
        SELECT h.crypto_name, h.quantity, h.coingecko_id,
               c.symbol, c.name as full_name, c.image_url
        FROM holdings h
        JOIN coins c ON h.coingecko_id = c.coin_id
        WHERE h.user_id = ? AND h.quantity > 0
        ORDER BY h.crypto_name ASC""",  # Added this line for A->Z sorting
        (user_id,)).fetchall()

    if request.method == "GET":
        # Fetch all available coins from the database and convert to list of dicts
        available_coins = [
            {
                'coin_id': row['coin_id'],
                'name': row['name'],
                'symbol': row['symbol'],
                'image_url': row['image_url']
            }
            for row in conn.execute("""
                SELECT coin_id, name, symbol, image_url
                FROM coins
                ORDER BY name""").fetchall()
        ]

        # Convert holdings to list of dicts - updated to include image_url
        holdings_with_info = [
            {
                'name': holding['crypto_name'],
                'quantity': holding['quantity'],
                'coingecko_id': holding['coingecko_id'],
                'symbol': holding['symbol'],
                'full_name': holding['full_name'],
                'logo': holding['image_url']  # Add the logo URL here
            }
            for holding in holdings
        ]

        conn.close()
        return render_template("trade.html",
                             holdings=holdings_with_info,
                             available_coins=available_coins)

    else:  # POST request
        from_crypto = request.form.get("from_crypto")
        to_crypto = request.form.get("to_crypto")
        amount = request.form.get("amount")

        if not from_crypto or not to_crypto or not amount:
            conn.close()
            return apology("Please fill in all fields")

        try:
            amount = float(amount)
            if amount <= 0:
                conn.close()
                return apology("Amount must be positive")
        except ValueError:
            conn.close()
            return apology("Invalid amount")

        # Get holding with CoinGecko ID
        holding = conn.execute("""
            SELECT h.quantity, h.coingecko_id, c.name, c.symbol
            FROM holdings h
            JOIN coins c ON h.coingecko_id = c.coin_id
            WHERE h.user_id = ? AND h.crypto_name = ?""",
            (user_id, from_crypto)).fetchone()

        if not holding or holding["quantity"] < amount:
            conn.close()
            return apology(f"You don't own enough {from_crypto}")

        # Get current prices for both cryptocurrencies
        from_crypto_data = fetch_single_coin(holding['coingecko_id'])
        to_crypto_data = fetch_single_coin(to_crypto)

        if not from_crypto_data or not to_crypto_data:
            conn.close()
            return apology("Error fetching cryptocurrency prices")

        # Calculate trade values
        from_value = amount * from_crypto_data["price"]
        to_amount = from_value / to_crypto_data["price"]

        # Calculate fee (0.15% of the transaction value)
        fee = from_value * TRANSACTION_FEE_RATE

        # Store trade details in session
        session["pending_trade"] = {
            "from_crypto": from_crypto,
            "from_crypto_id": holding['coingecko_id'],
            "to_crypto": to_crypto_data['name'],
            "to_crypto_id": to_crypto,
            "from_amount": amount,
            "to_amount": to_amount,
            "from_price": from_crypto_data["price"],
            "to_price": to_crypto_data["price"],
            "fee": fee,
            "total_value": from_value,
            "from_symbol": holding['symbol'],
            "to_symbol": to_crypto_data["symbol"]
        }

        conn.close()
        return render_template("confirm_trade.html")


@app.route("/confirm_trade", methods=["POST"])
@login_required
def confirm_trade():
    # Get the pending trade from session
    trade = session.get("pending_trade")
    if not trade:
        return apology("No pending trade")

    conn = get_db_connection()
    try:
        user_id = session["user_id"]

        # Verify user still has enough cryptocurrency
        holding = conn.execute("""
            SELECT quantity
            FROM holdings
            WHERE user_id = ? AND crypto_name = ?""",
            (user_id, trade["from_crypto"])).fetchone()

        if not holding or holding["quantity"] < trade["from_amount"]:
            session.pop("pending_trade", None)
            conn.close()
            return apology(f"Insufficient {trade['from_crypto']} balance")

        # Begin transaction
        conn.execute("BEGIN TRANSACTION")

        # Record the trade transaction
        conn.execute("""
            INSERT INTO transactions
            (user_id, crypto_name, quantity, price, fee, type, trade_to_crypto, trade_to_quantity, coingecko_id)
            VALUES (?, ?, ?, ?, ?, 'trade', ?, ?, ?)""",
            (user_id, trade["from_crypto"], trade["from_amount"],
             trade["from_price"], trade["fee"], trade["to_crypto"],
             trade["to_amount"], trade["from_crypto_id"]))

        # Update holdings for the source cryptocurrency
        new_from_quantity = holding["quantity"] - trade["from_amount"]
        if new_from_quantity > 0:
            conn.execute("""
                UPDATE holdings
                SET quantity = ?
                WHERE user_id = ? AND crypto_name = ?""",
                (new_from_quantity, user_id, trade["from_crypto"]))
        else:
            conn.execute("""
                DELETE FROM holdings
                WHERE user_id = ? AND crypto_name = ?""",
                (user_id, trade["from_crypto"]))

        # Update or insert holdings for the target cryptocurrency
        existing_to_holding = conn.execute("""
            SELECT quantity
            FROM holdings
            WHERE user_id = ? AND crypto_name = ?""",
            (user_id, trade["to_crypto"])).fetchone()

        if existing_to_holding:
            conn.execute("""
                UPDATE holdings
                SET quantity = quantity + ?
                WHERE user_id = ? AND crypto_name = ?""",
                (trade["to_amount"], user_id, trade["to_crypto"]))
        else:
            conn.execute("""
                INSERT INTO holdings (user_id, crypto_name, quantity, coingecko_id)
                VALUES (?, ?, ?, ?)""",
                (user_id, trade["to_crypto"], trade["to_amount"], trade["to_crypto_id"]))

        # Commit transaction
        conn.commit()

        # Clear the pending trade from session
        session.pop("pending_trade", None)

        flash(f"Successfully traded {format_crypto_quantity(trade['from_amount'])} {trade['from_symbol']} for {format_crypto_quantity(trade['to_amount'])} {trade['to_symbol']}")

        conn.close()
        return redirect("/")

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        return apology(f"Trade failed: {str(e)}")



@app.route("/login", methods=["GET", "POST"])
def login():
    # Don't clear session at the start - this erases flash messages
    #if request.method == "GET":
    #   return render_template("login.html")

    # Clear password reset flag if it exists
    session.pop('password_reset_success', None)

    # User reached route via POST (submitting a form)
    if request.method == "POST":
        # Clear session only when processing login attempt
        session.clear()

        # Ensure username and password were submitted
        username = request.form.get("username")
        password = request.form.get("password")

        if not username:
            flash("Must provide username.", "danger")
            return redirect("/login")

        if not password:
            flash("Must provide password.", "danger")
            return redirect("/login")

        # Query database for username
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username,)).fetchall()

        # Ensure username exists and password is correct
        if len(rows) != 1 or not check_password_hash(rows[0]["hash"], password):
            return apology("invalid username and/or password")

        # Remember which user has logged in
        session["user_id"] = rows[0]["id"]
        # Add username to session
        session["username"] = rows[0]["username"]

        # Redirect user to home page
        return redirect("/")

    return render_template("login.html")



@app.route("/logout")
def logout():
    """Log user out"""

    # Forget any user_id and username
    session.clear()

    # Redirect user to login form
    return redirect("/")


# Route to handle clearing the history
@app.route("/clear_quotes", methods=["POST"])
@login_required
def clear_quotes():
    session.pop('quotes_history', None)
    return redirect("/quote")

@app.route("/terms")
def terms():
    # Path to your Markdown file
    terms_path = os.path.join(os.path.dirname(__file__), 'terms_of_use.md')

    try:
        # Read the Markdown file
        with open(terms_path, 'r') as file:
            terms_markdown = file.read()

        # Convert Markdown to HTML
        terms_html = markdown.markdown(terms_markdown, extensions=['fenced_code', 'tables'])

        return render_template("terms.html", terms_content=terms_html)
    except FileNotFoundError:
        # Fallback if file is not found
        return render_template("terms.html", terms_content="<p>Terms of Use are currently unavailable.</p>")


# Signup route
@app.route("/register", methods=["GET", "POST"])
def register():
    """Register user"""
    session.clear()

    security_questions = [
        "What was your first pet's name?",
        "In which city were you born?",
        "What was your childhood nickname?",
        "What is your mother's maiden name?",
        "What was the name of your first school?",
        "What is the make of your first car?",
        "What is your favorite book?",
        "What street did you grow up on?"
    ]

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirmation = request.form.get("confirmation")
        terms_accepted = request.form.get("terms_accepted")

        # Get all three security questions and answers
        questions = []
        answers = []
        for i in range(1, 4):
            q = request.form.get(f"security_question{i}")
            a = request.form.get(f"security_answer{i}")
            questions.append(q)
            answers.append(a)

        # Validate all inputs
        if not username:
            return apology("must provide username")
        if not password:
            return apology("must provide password")
        if not confirmation:
            return apology("must confirm password")
        if password != confirmation:
            return apology("passwords must match")
        if not terms_accepted:
            return apology("must accept terms of use")

        # Validate security questions
        if len(set(questions)) != 3:
            return apology("must select three different security questions")
        for i, answer in enumerate(answers, 1):
            if not answer:
                return apology(f"must provide answer for security question {i}")

        conn = get_db_connection()
        try:
            # Check if username exists
            if conn.execute("SELECT * FROM users WHERE username = ?",
                          (username,)).fetchone():
                return apology("username already exists")

            # Begin transaction
            conn.execute("BEGIN TRANSACTION")

            # Insert user
            conn.execute(
                "INSERT INTO users (username, hash) VALUES (?, ?)",
                (username, generate_password_hash(password))
            )

            # Get new user's ID
            user_id = conn.execute(
                "SELECT id FROM users WHERE username = ?",
                (username,)
            ).fetchone()["id"]

            # Insert security questions
            conn.execute("""
                INSERT INTO security_questions
                (user_id, question1, answer1, question2, answer2, question3, answer3)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, questions[0], answers[0].lower(),
                 questions[1], answers[1].lower(),
                 questions[2], answers[2].lower())
            )

            conn.commit()
            #session["user_id"] = user_id
            flash(f"Registered successfully!")
            return redirect("/login")

        except Exception as e:
            conn.rollback()
            return apology(f"Registration failed: {str(e)}")
        finally:
            conn.close()

    return render_template("register.html",
                         security_questions=security_questions,
    )



@app.route("/sell", methods=["GET", "POST"])
@login_required
def sell():
    conn = get_db_connection()
    user_id = session["user_id"]

    # Get holdings with CoinGecko IDs - Added ORDER BY for alphabetical sorting
    holdings = conn.execute("""
        SELECT crypto_name, quantity, coingecko_id
        FROM holdings
        WHERE user_id = ? AND quantity > 0
        ORDER BY crypto_name ASC""",  # Added this line for A->Z sorting
        (user_id,)).fetchall()

    if request.method == "GET":
        conn.close()
        # Get current prices for holdings
        holdings_with_prices = []
        for holding in holdings:
            crypto_data = lookup(holding['coingecko_id'])
            if crypto_data:
                holdings_with_prices.append({
                    'crypto_name': holding['crypto_name'],
                    'quantity': holding['quantity'],
                    'current_price': crypto_data['price'],
                    'value': holding['quantity'] * crypto_data['price'],
                    'coingecko_id': holding['coingecko_id']
                })

        # The list is already sorted thanks to the SQL query
        return render_template("sell.html", holdings=holdings_with_prices)


    else: # POST request
        crypto_name = request.form.get("crypto_name")
        sell_type = request.form.get("sellType")

        # Get the appropriate amount based on sell type
        if sell_type == "dollars":
            dollars = request.form.get("dollars")
            try:
                dollars = float(dollars)
                if dollars <= 0:
                    return apology("Amount must be positive")

                # Get crypto data to calculate amount
                holding = conn.execute("""
                    SELECT quantity, coingecko_id
                    FROM holdings
                    WHERE user_id = ? AND crypto_name = ?""",
                    (user_id, crypto_name)).fetchone()

                crypto_data = lookup(holding['coingecko_id'])
                if not crypto_data:
                    return apology("Error fetching cryptocurrency price")

                amount = dollars / crypto_data["price"]
                total_value = dollars

            except ValueError:
                return apology("Invalid dollar amount")
        else:
            amount = request.form.get("amount")
            try:
                amount = float(amount)
                if amount <= 0:
                    return apology("Amount must be positive")
            except ValueError:
                return apology("Invalid amount")

        # Get holding with CoinGecko ID
        holding = conn.execute("""
            SELECT quantity, coingecko_id
            FROM holdings
            WHERE user_id = ? AND crypto_name = ?""",
            (user_id, crypto_name)).fetchone()

        if not holding or holding["quantity"] < amount:
            conn.close()
            return apology("You do not own enough of this cryptocurrency")

        # Get current price using CoinGecko ID
        crypto_data = lookup(holding['coingecko_id'])
        if not crypto_data:
            conn.close()
            return apology("Error fetching cryptocurrency price")

        # Calculate total sale value
        total_value = amount * crypto_data["price"]

        # Store sale details in session
        session["pending_sale"] = {
            "crypto_name": crypto_name,
            "coingecko_id": holding['coingecko_id'],
            "amount": amount,
            "price": crypto_data["price"],
            "total_value": total_value,
            "image_url": crypto_data["image_url"]
        }

        conn.close()
        return render_template("confirm_sell.html")


@app.route("/confirm_sell", methods=["POST"])
@login_required
def confirm_sell():
    sale = session.get("pending_sale")
    if not sale:
        return apology("No pending sale")

    try:
        conn = get_db_connection()
        user_id = session["user_id"]

        # Calculate transaction fee
        transaction_fee = sale["total_value"] * TRANSACTION_FEE_RATE
        net_proceeds = sale["total_value"] - transaction_fee

        # Begin transaction
        conn.execute("BEGIN TRANSACTION")

        # Update user's cash balance (minus fee)
        conn.execute("""
            UPDATE users
            SET cash = cash + ?
            WHERE id = ?""",
            (net_proceeds, user_id))

        # Record the transaction with fee
        conn.execute("""
            INSERT INTO transactions
            (user_id, crypto_name, quantity, price, fee, type)
            VALUES (?, ?, ?, ?, ?, 'sell')""",
            (user_id, sale["crypto_name"], sale["amount"],
             sale["price"], transaction_fee))

        # Update holdings
        # First get current holding quantity
        current_holding = conn.execute("""
            SELECT quantity
            FROM holdings
            WHERE user_id = ? AND crypto_name = ?""",
            (user_id, sale["crypto_name"])).fetchone()

        new_quantity = current_holding["quantity"] - sale["amount"]

        if new_quantity > 0:
            # Update the holding with new quantity
            conn.execute("""
                UPDATE holdings
                SET quantity = ?
                WHERE user_id = ? AND crypto_name = ?""",
                (new_quantity, user_id, sale["crypto_name"]))
        else:
            # Delete the holding if quantity is 0
            conn.execute("""
                DELETE FROM holdings
                WHERE user_id = ? AND crypto_name = ?""",
                (user_id, sale["crypto_name"]))

        conn.commit()
        session.pop("pending_sale", None)

        flash(f"Successfully sold {format_crypto_quantity(sale['amount'])} {sale['crypto_name']} for {format_crypto_price(sale['total_value'])} (Fee: {format_crypto_price(transaction_fee)})")

        conn.close()
        return redirect("/")

    except Exception as e:
        conn.rollback()
        conn.close()
        return apology(f"Sale failed: {str(e)}")


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        # Fetch user's security questions
        db = get_db_connection()
        questions = db.execute("""
            SELECT question1, question2, question3
            FROM security_questions
            WHERE user_id = ?
        """, (session["user_id"],)).fetchone()

        if not questions:
            flash("Security questions not found!", "danger")
            return redirect(url_for("index"))

        # Randomly select one question number (1, 2, or 3)
        question_num = random.randint(1, 3)

        # Get the randomly selected question
        selected_question = questions[f'question{question_num}']

        # Store the selected question number in session for verification
        session['security_question_num'] = question_num

        return render_template("change_password.html", security_question=selected_question)

    # POST method
    db = get_db_connection()

    # Validate inputs
    current_password = request.form.get("current_password")
    new_password = request.form.get("new_password")
    confirm_new_password = request.form.get("confirm_new_password")
    security_answer = request.form.get("security_answer", "").strip().lower()

    # Check if passwords match
    if new_password != confirm_new_password:
        flash("New passwords do not match!", "danger")
        return redirect(url_for("change_password"))

    # Validate current password
    user = db.execute("SELECT hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not check_password_hash(user['hash'], current_password):
        flash("Current password is incorrect!", "danger")
        return redirect(url_for("change_password"))

    # Get the question number from session
    question_num = session.get('security_question_num')
    if not question_num:
        flash("Session expired. Please try again.", "danger")
        return redirect(url_for("change_password"))

    # Get stored security answer for the selected question
    security = db.execute(f"""
        SELECT answer{question_num}
        FROM security_questions
        WHERE user_id = ?
    """, (session["user_id"],)).fetchone()

    # Validate security answer
    if security_answer.lower() != security[f'answer{question_num}'].lower():
        flash("Security answer is incorrect.", "danger")
        return redirect(url_for("change_password"))

    # Update password
    hash = generate_password_hash(new_password)
    db.execute("UPDATE users SET hash = ? WHERE id = ?", (hash, session["user_id"]))
    db.commit()

    # Clear the security question number from session
    session.pop('security_question_num', None)

    flash("Password successfully changed!", "success")
    return redirect(url_for("index"))



@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    # Handle POST request
    username = request.form.get("username")

    # Validate username exists
    db = get_db_connection()
    user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()

    if not user:
        flash("Username not found!", "danger")
        return redirect(url_for("forgot_password"))

    # Store user_id in session for the next step
    session['reset_user_id'] = user['id']

    # Redirect to security questions
    return redirect(url_for("verify_security_questions"))



@app.route("/verify_security_questions", methods=["GET", "POST"])
def verify_security_questions():
    # Ensure there's a user trying to reset password
    if 'reset_user_id' not in session:
        return redirect(url_for("login"))

    db = get_db_connection()

    if request.method == "GET":
        # Get the security questions for the user
        questions = db.execute("""
            SELECT question1, question2, question3
            FROM security_questions
            WHERE user_id = ?
        """, (session['reset_user_id'],)).fetchone()

        return render_template("verify_security.html", questions=questions)

    # Handle POST request
    answers = {
        1: request.form.get("answer1", "").strip().lower(),
        2: request.form.get("answer2", "").strip().lower(),
        3: request.form.get("answer3", "").strip().lower()
    }

    # Get stored answers
    stored = db.execute("""
        SELECT answer1, answer2, answer3
        FROM security_questions
        WHERE user_id = ?
    """, (session['reset_user_id'],)).fetchone()

    # Check answers (require all 3 for password reset)
    correct_answers = sum(
        1 for i in range(1, 4)
        if answers[i] == stored[f'answer{i}'].lower()
    )

    if correct_answers < 3:
        flash("Incorrect security answers. Please try again.", "danger")
        return redirect(url_for("verify_security_questions"))

    # If all answers correct, allow password reset
    return redirect(url_for("reset_password"))



@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    if 'reset_user_id' not in session:
        flash("Reset session expired", "danger")
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("reset_password.html")

    new_password = request.form.get("new_password")
    confirm_password = request.form.get("confirm_password")

    if new_password != confirm_password:
        flash("Passwords do not match!", "danger")
        return redirect(url_for("reset_password"))

    try:
        db = get_db_connection()
        hash = generate_password_hash(new_password)

        db.execute("UPDATE users SET hash = ? WHERE id = ?",
                   (hash, session['reset_user_id']))
        db.commit()

        # Only flash if we haven't already
        if 'password_reset_success' not in session:
            flash("Password has been reset successfully! Please log in.", "success")
            session['password_reset_success'] = True

        session.pop('reset_user_id', None)

        return redirect(url_for("login"))

    except Exception as e:
        flash(f"An error occurred: {str(e)}", "danger")
        return redirect(url_for("reset_password"))


@app.route("/add_cash")
@login_required
def add_cash():
    return render_template("add_cash.html")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    # Connect to database
    conn = get_db_connection()
    db = conn.cursor()

    if request.method == "POST":
        # Get form data
        full_name = request.form.get("full_name")
        bio = request.form.get("bio")
        education_level = request.form.get("education_level")
        field_of_study = request.form.get("field_of_study")
        years_trading = request.form.get("years_trading")
        interests = request.form.get("interests")
        linkedin_url = request.form.get("linkedin_url")
        twitter_url = request.form.get("twitter_url")
        location = request.form.get("location")
        website = request.form.get("website")

        # Handle profile picture upload
        profile_picture = None
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file.filename != '':
                # Ensure it's an image file
                if file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    # Generate unique filename
                    filename = secure_filename(f"profile_{session['user_id']}_{int(datetime.now().timestamp())}{os.path.splitext(file.filename)[1]}")
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    profile_picture = filename

        try:
            # Check if profile exists
            db.execute("SELECT id FROM profile WHERE user_id = ?", (session["user_id"],))
            existing_profile = db.fetchone()

            if existing_profile:
                # Update existing profile
                db.execute("""
                    UPDATE profile
                    SET full_name = ?, bio = ?, education_level = ?, field_of_study = ?,
                        years_trading = ?, interests = ?, linkedin_url = ?, twitter_url = ?,
                        location = ?, website = ?, updated_at = CURRENT_TIMESTAMP
                        {}
                    WHERE user_id = ?
                """.format(", profile_picture = ?" if profile_picture else ""),
                    (full_name, bio, education_level, field_of_study, years_trading,
                     interests, linkedin_url, twitter_url, location, website,
                     *([profile_picture] if profile_picture else []), session["user_id"]))
            else:
                # Create new profile
                db.execute("""
                    INSERT INTO profile
                    (user_id, full_name, bio, education_level, field_of_study,
                     years_trading, interests, linkedin_url, twitter_url,
                     location, website, profile_picture)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (session["user_id"], full_name, bio, education_level, field_of_study,
                      years_trading, interests, linkedin_url, twitter_url,
                      location, website, profile_picture))

            conn.commit()
            flash("Profile updated successfully!", "success")

        except sqlite3.Error as e:
            conn.rollback()
            flash("An error occurred while updating your profile.", "error")
            print(f"Database error: {e}")

        finally:
            conn.close()

        return redirect(url_for('profile'))

    else:  # GET request
        # Fetch existing profile data
        db.execute("SELECT * FROM profile WHERE user_id = ?", (session["user_id"],))
        profile_data = db.fetchone()
        conn.close()

        return render_template("profile.html", profile=profile_data)
