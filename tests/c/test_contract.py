from pcc.support.util import Base, PositiveInteger


dx: PositiveInteger


class Player2(Base):
    # 3.6 class anotations

    x: PositiveInteger    # but it can be work

    def left(self, dx):
        self.x -= dx


def test_contact():

    a = Player2(2)  # init can be checked abut a.x can be check?
    # when Player2(1) 1-1 is 0 so it is failed!
    a.left(1)