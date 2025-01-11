from datetime import datetime
import json
from decimal import Decimal


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)

        if isinstance(obj, datetime):
            return obj.isoformat()

            # if obj == obj.to_integral_value():
            #     return "{:.1f}".format(obj)

            # return str(obj)
        return super(DecimalEncoder, self).default(obj)
