from flask import Flask, render_template

app = Flask(__name__)


def sample_products():
    """Return fake product pricing for the prototype."""
    return [
        {
            "product": "Chicken Breast, Boneless 40 lb",
            "sysco": 128.50,
            "us_foods": 131.20,
            "pfg": 129.75,
        },
        {
            "product": "Ground Beef 80/20 10 lb",
            "sysco": 54.99,
            "us_foods": 56.25,
            "pfg": 53.80,
        },
        {
            "product": "Frozen French Fries 6/5 lb",
            "sysco": 42.10,
            "us_foods": 40.95,
            "pfg": 43.20,
        },
        {
            "product": "Mozzarella Cheese Shredded 30 lb",
            "sysco": 99.40,
            "us_foods": 101.00,
            "pfg": 98.75,
        },
        {
            "product": "Tomato Sauce #10 Can (Case)",
            "sysco": 28.30,
            "us_foods": 29.10,
            "pfg": 27.95,
        },
        {
            "product": "Burger Buns 8-count (10 packs)",
            "sysco": 24.50,
            "us_foods": 23.80,
            "pfg": 24.10,
        },
        {
            "product": "Romaine Lettuce Chopped 4/5 lb",
            "sysco": 37.25,
            "us_foods": 38.40,
            "pfg": 36.90,
        },
        {
            "product": "Bacon Sliced 15 lb",
            "sysco": 72.60,
            "us_foods": 74.20,
            "pfg": 71.85,
        },
        {
            "product": "Eggs Large Grade A (15 dozen)",
            "sysco": 44.90,
            "us_foods": 43.95,
            "pfg": 45.10,
        },
        {
            "product": "Paper To-Go Containers 200 ct",
            "sysco": 31.75,
            "us_foods": 32.40,
            "pfg": 30.95,
        },
    ]


@app.route("/")
def index():
    return render_template("index.html", products=sample_products())


if __name__ == "__main__":
    app.run(debug=True)
