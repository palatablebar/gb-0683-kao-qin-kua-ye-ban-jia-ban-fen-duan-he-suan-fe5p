from flask import Blueprint

from app.web import api, page

bp = Blueprint('m8_monthly', __name__)


@bp.get('/m8_monthly')
def screen():
    return page(8, 'm8_monthly.html')


@bp.route('/api/m8_monthly', methods=['GET', 'POST'])
def preview():
    return api(8)
