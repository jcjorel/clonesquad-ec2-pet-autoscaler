import os
import logging
NOTICE = 25
logging.addLevelName(NOTICE,"NOTICE")


def logger(name):
    logger        = logging.getLogger(name)
    logger.NOTICE = NOTICE
    logger.DEBUG  = logging.DEBUG
    
    log_spec = None
    if "CLONESQUAD_LOGLEVELS" in os.environ:
        log_spec = {}
        for spec in os.environ["CLONESQUAD_LOGLEVELS"].split(","):
            k, v = spec.split("=")
            log_spec[k] = v
    is_sam_local = "AWS_SAM_LOCAL" in os.environ and os.environ["AWS_SAM_LOCAL"] == "true"

    log_level = logging.DEBUG if is_sam_local else logging.INFO 
    if log_spec is not None and (name in log_spec or "*" in log_spec):
        module_log_spec = log_spec[name] if name in log_spec else log_spec["*"]
        level = getattr(logging, module_log_spec, None)
        if level is None:
            level = getattr(logger, module_log_spec, None)
        if not isinstance(level, int):
           logger.warning('Invalid log level: %s' % level)
        else:
            log_level = level

    logger.setLevel(log_level)
    logger.propagate = False

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(log_level)

    # create formatter
    extra_logging = "%(asctime)s - " if is_sam_local else ""
    formatter = logging.Formatter("[%%(levelname)s] %s%%(filename)s:%%(lineno)d - %%(message)s" % extra_logging)

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)
    return logger

