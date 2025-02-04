from my_research.tools.registry import Registry

MODEL_REGISTRY = Registry('MODEL')
DATA_REGISTRY = Registry('DATA')


def build_model(cfg):
    """
    Built the whole model, defined by `cfg.MODEL.NAME`.
    """
    # meta_arch = cfg.MODEL.META_ARCHITECTURE
    # cfg['MODEL']['NAME'] == 'MobRecon_DS'
    return MODEL_REGISTRY.get(cfg['MODEL']['NAME'])(cfg)


def build_dataset(cfg, phase, **kwargs):
    """
    Built the whole model, defined by `cfg.TRAIN.DATASET`.
    """
    # meta_arch = cfg.MODEL.META_ARCHITECTURE
    #                   .get('MultipleDatasets')
    print(f'Build Dataset: {phase}, {cfg[phase.upper()]["DATASET"]}')
    return DATA_REGISTRY.get(cfg[phase.upper()]['DATASET'])(cfg, phase, **kwargs)
