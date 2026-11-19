"""Built-in editing starting points. Source regions are chosen by the editor."""
from composition import normalize_layout_config


def builtin_presets() -> list[dict]:
    items = [
        ("vertical-blur", "Vertical com fundo", "Vídeo inteiro com fundo desfocado.", "blur", {}, False),
        ("vertical-full", "Vertical em tela cheia", "Preenche o quadro com recorte central.", "crop", {}, False),
        ("horizontal-full", "Horizontal completo", "Preserva o vídeo no formato 16:9.", "fit", {"aspect": "16:9"}, False),
        ("gaming-captions", "Game com legenda central", "Rosto em cima, faixa de legenda e jogo embaixo.", "fit",
         {"mode": "split_caption", "caption_position": "middle"}, True),
        ("gaming-split", "Game em duas telas", "Rosto em cima e jogo embaixo, sem faixa central.", "fit", {"mode": "split"}, False),
        ("horizontal-split", "Duas telas horizontais", "Rosto e jogo empilhados em um vídeo 16:9.", "fit",
         {"mode": "split", "aspect": "16:9"}, False),
        ("pip-right", "Rosto na lateral direita", "Janela do rosto sobre o vídeo horizontal.", "fit",
         {"mode": "pip", "aspect": "16:9", "pip_position": "top-right"}, False),
        ("pip-left", "Rosto na lateral esquerda", "Janela do rosto sobre o vídeo vertical.", "fit",
         {"mode": "pip", "pip_position": "top-left"}, False),
    ]
    return [{"id": key, "name": name, "description": description, "layout": layout,
             "layout_config": normalize_layout_config(config), "subtitles": subtitles}
            for key, name, description, layout, config, subtitles in items]
