from app import app,db,Order
app.app_context().push()
print(Order.query.all())
