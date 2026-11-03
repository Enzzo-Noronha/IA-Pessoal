import sys

from ui.app import App

if __name__ == "__main__":
    sys.setswitchinterval(0.001)  # a janela volta a ter vez com mais frequência quando há trabalho em segundo plano
    App().mainloop()
